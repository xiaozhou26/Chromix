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
            elif args[:1] == [str(repo / "tools/platform_pins.py")]:
                kind = "pins"
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
        shutil.copy2(REPO / "tools/platform_pins.py", tools / "platform_pins.py")
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
            from platform_pins import load_pins
            version = load_pins(repo, os.environ["TEST_PLATFORM"])["ChromiumVersion"]
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
                         gate_failure=None, launcher_failure=None, build_failure=None,
                         package_failure=None, extract_failure=False, macos_opt_in=False):
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
        version = "153.0.8010.36" if platform == "linux" else "152.0.7977.82"
        (repo / "CHROMIUM_VERSION").write_text("152.0.7977.82\n")
        (repo / "CHROMIUM_LINUX_VERSION").write_text("153.0.8010.36\n")
        (repo / "build/ungoogled-revisions.psd1").write_text(
            '@{\n  ChromiumVersion = "152.0.7977.82"\n'
            '  UngoogledVersion = "152.0.7977.82-1"\n'
            f'  UngoogledCommit = "{"a" * 40}"\n'
            '  LinuxChromiumVersion = "153.0.8010.36"\n'
            '  LinuxUngoogledVersion = "153.0.8010.36-1"\n'
            f'  LinuxUngoogledCommit = "{"b" * 40}"\n'
            '  UngoogledLinuxVersion = "153.0.8010.36-1"\n'
            f'  UngoogledLinuxCommit = "{"c" * 40}"\n'
            '  UngoogledMacOSVersion = "152.0.7977.82-1.1"\n'
            f'  UngoogledMacOSCommit = "{"d" * 40}"\n}}\n')
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
        build.write_text(f"#!/bin/sh\nexit {7 if build_failure == 'nonzero' else 0}\n")
        if build_failure == "missing":
            (out / "chrome").unlink()
        build.chmod(0o755)
        seed = root / "seed"
        seed.mkdir()
        launch_log = root / "launcher-log"
        marker = "<p>missing</p>" if smoke_failure == "marker" else "<p>chromix-smoke-ok</p>"
        exit_code = 7 if smoke_failure == "nonzero" else 0
        launch_version = "0.0.0.0" if smoke_failure == "version_mismatch" else version
        version_exit = 9 if smoke_failure == "version_nonzero" else 0
        launcher = ('#!/bin/sh\nprintf "%s\\n" "$*" >> "$LAUNCH_TEST_LOG"\n'
                    f'case "$1" in --version) printf "Chromix {launch_version}\\n"; exit {version_exit};; '
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
        if extract_failure:
            (seed / asset).write_bytes(b"not a ZIP archive")
        checksum = hashlib.sha256((seed / asset).read_bytes()).hexdigest()
        if corrupt:
            checksum = "0" * 64
        (seed / "SHA256SUMS").write_text(f"{checksum}  {asset}\n")
        package.parent.mkdir(parents=True, exist_ok=True)
        package_script = '#!/bin/sh\ncp "$TEST_SEED"/* "$2/"\n'
        package_script += {
            "nonzero": "exit 13\n",
            "missing": 'rm "$2/' + asset + '"\n',
            "empty": ': > "$2/' + asset + '"\n',
            "checksum_missing": 'rm "$2/SHA256SUMS"\n',
            "checksum_entry": 'printf "invalid\\n" > "$2/SHA256SUMS"\n',
            "checksum_duplicate": 'cat "$TEST_SEED/SHA256SUMS" >> "$2/SHA256SUMS"\n',
            "checksum_malformed": 'printf "invalid\\n" >> "$2/SHA256SUMS"\n',
            "checksum_wrong_name": ('mv "$2/' + asset + '" "$2/' + asset.replace('.zip', 'Xzip') + '"\n'
                                    'sed -i "s/\\.zip/Xzip/" "$2/SHA256SUMS"\n'
                                    'cp "$TEST_SEED/' + asset + '" "$2/"\n'),
        }.get(package_failure, "")
        package.write_text(package_script)
        package.chmod(0o755)
        if platform == "linux":
            (repo / "build/linux/prepare-ci-sandbox.sh").write_text(
                '#!/bin/sh\nprintf "sandbox-preflight\\n" >> "$LAUNCH_TEST_LOG"\n'
                f'exit {11 if smoke_failure == "sandbox" else 0}\n')
        github_output = root / "github_output"
        env = {key: value for key, value in os.environ.items() if not key.startswith("CHROMIX_")}
        env.update({"TEST_SEED": str(seed), "LAUNCH_TEST_LOG": str(launch_log),
                    "GITHUB_OUTPUT": str(github_output), "TEST_GATE_FAILURE": gate_failure or "",
                    "TEST_REAL_TIMEOUT": shutil.which("timeout"), "TEST_ASSET": asset,
                    "TEST_BROWSER_RELATIVE": native_relative, "TEST_PLATFORM": platform,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONOPTIMIZE": "0", "PIP_NO_INDEX": "1", "PIP_CONFIG_FILE": os.devnull})
        env["CHROMIX_MACOS_COMPILE_RELEASE"] = str(int(macos_opt_in))
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
        initial_outputs = ["status=running", "finished=false"]
        compiled_outputs = initial_outputs + ["compiled_ready=true", "runtime_verified=false"]
        ready_outputs = compiled_outputs + ["package_ready=true"]
        if platform == "macos":
            ready_outputs.append("runtime_verified=false")
        if build_failure or package_failure:
            self.assertNotEqual(result.returncode, 0, diagnostic)
            self.assertEqual(output_lines, initial_outputs if build_failure else compiled_outputs)
            self.assertEqual([call["kind"] for call in runtime_calls], expected_calls)
            self.assertFalse(launch_log.exists())
            self.assertFalse((work / "smoke").exists())
            self.assertFalse(list(work.glob(".snapshot-stage-*")))
            self.assertTrue((work / "src/.chromix-source-ready").is_file())
            return
        if not corrupt:
            expected_calls.append("extract")
            if not (unsafe_member or launcher_failure or extract_failure):
                if cross:
                    expected_calls.append("static")
                else:
                    expected_calls.append("pins")
                    if platform == "macos":
                        expected_calls.append("smoke")
                    elif smoke_failure != "sandbox":
                        expected_calls.append("timeout")
                        if smoke_failure not in ("version_nonzero", "version_mismatch"):
                            expected_calls.append("timeout")
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
            self.assertEqual(gate_calls[0]["outputs"], ready_outputs)
            self.assertIn("fixture fingerprint: inputs verified", result.stdout)
            if gate_failure:
                self.assertNotIn("fixture fingerprint: passed", diagnostic)
            else:
                self.assertIn("fixture fingerprint: passed", result.stdout)
        else:
            self.assertEqual(receipts, [])
            self.assertNotIn("fixture fingerprint:", diagnostic)

        if platform == "macos" and not (corrupt or unsafe_member or launcher_failure or extract_failure):
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
                    "status=running", "finished=false", "compiled_ready=true", "runtime_verified=false",
                    "package_ready=true", "runtime_verified=false"])
        else:
            self.assertFalse((work / "runtime-smoke-stage-1").exists())

        if corrupt:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("bundle checksum verification failed", result.stderr)
            self.assertEqual(output["finished"], "false")
            self.assertFalse(launch_log.exists())
            self.assertFalse((work / "smoke").exists())
        elif unsafe_member or launcher_failure or extract_failure:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("File is not a zip file" if extract_failure else
                          "Unsafe ZIP path" if unsafe_member else "extracted bundle launcher is missing",
                          result.stderr)
            self.assertEqual(output["finished"], "false")
            self.assertFalse(launch_log.exists())
            self.assertFalse((work / "smoke/chromix/chromix").exists())
            self.assertEqual(output_lines, ready_outputs + ["runtime_failed=true"])
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
            self.assertEqual(output_lines, ready_outputs + ["runtime_failed=true"])
            if smoke_failure:
                if platform == "macos":
                    code, message = 1, "extracted macOS runtime smoke test failed; see runtime-smoke-stage-1"
                else:
                    code, message = {
                        "nonzero": (7, "extracted headless smoke test failed"),
                        "marker": (1, "smoke page marker missing from dumped DOM"),
                        "version_nonzero": (9, "extracted launcher --version check failed"),
                        "version_mismatch": (1, "extracted browser version does not match the pinned Chromium version"),
                        "sandbox": (11, "Linux sandbox preparation failed"),
                    }[smoke_failure]
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
        if not (corrupt or unsafe_member or launcher_failure or extract_failure or verifier_failure or cross):
            calls = launch_log.read_text()
            if smoke_failure == "sandbox":
                self.assertEqual(calls, "sandbox-preflight\n")
            else:
                if platform == "linux":
                    self.assertLess(calls.index("sandbox-preflight"), calls.index("--version"))
                self.assertIn("--version", calls)
                if smoke_failure in ("version_nonzero", "version_mismatch"):
                    self.assertNotIn("--headless", calls)
                else:
                    self.assertIn("--headless", calls)
        if corrupt:
            self.assertEqual(output_lines, compiled_outputs)
        elif verifier_failure:
            self.assertEqual(output_lines, ready_outputs)

    def test_linux_ignores_macos_compile_release_opt_in(self):
        for failure in ("nonzero", "timeout", "dependencies"):
            with self.subTest(failure=failure):
                self.complete_fixture("linux", False, gate_failure=failure, macos_opt_in=True)

    def test_linux_fingerprint_failures_preserve_checked_bundle_without_finishing(self):
        for failure in ("nonzero", "timeout", "dependencies", "hash"):
            with self.subTest(failure=failure):
                self.complete_fixture("linux", False, gate_failure=failure)

    def test_linux_smoke_failures_preserve_checked_bundle_without_finishing(self):
        for failure in ("nonzero", "marker", "version_nonzero", "version_mismatch", "sandbox"):
            with self.subTest(failure=failure):
                self.complete_fixture("linux", False, smoke_failure=failure)

    def test_linux_hash_checked_archive_extraction_failure_is_diagnostic_only(self):
        self.complete_fixture("linux", False, extract_failure=True)

    def test_linux_launcher_failures_preserve_checked_bundle(self):
        for failure in ("missing", "not_executable"):
            with self.subTest(failure=failure):
                self.complete_fixture("linux", False, launcher_failure=failure)

    def test_linux_compile_and_package_failures_never_claim_package_ready(self):
        for failure in ("nonzero", "missing"):
            with self.subTest(build_failure=failure):
                self.complete_fixture("linux", False, build_failure=failure)
        for failure in ("nonzero", "missing", "empty", "checksum_missing", "checksum_entry",
                        "checksum_duplicate", "checksum_malformed", "checksum_wrong_name"):
            with self.subTest(package_failure=failure):
                self.complete_fixture("linux", False, package_failure=failure)

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


MACOS_COMPILE_RELEASE_DISPATCH = '''  # Frozen mac156 continuations retain their original blocking runtime contract.
  if [ -z "$UNCHANGED_HELPER" ] && { [[ "$CHROMIUM_VERSION_PIN" = 157.* ]] ||
      [ "${CHROMIX_MACOS_COMPILE_RELEASE:-0}" = 1 ]; }; then
    finish_macos_compile_release
  fi
'''


class MacOSCompileReleaseTest(unittest.TestCase):
    """Real stage, packager, CRC/Mach-O/source verifiers; only build/probes are offline doubles."""

    def fixture(self, *, arch="x64", version="157.0.8120.1", opt_in=False,
                failure="fingerprint", damage=None, frozen=False):
        import plistlib
        from tools.tests.test_verify_macos_bundle import MacOSBundleTest
        from tools.tests.test_verify_patch_stack import source_tree, stack

        temporary = tempfile.TemporaryDirectory(prefix="mac compile release ")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        repo, source = source_tree(root)
        work = root / "work"
        work.mkdir()
        src = work / "src"
        source.rename(src)
        (repo / "tools").symlink_to(REPO / "tools", target_is_directory=True)
        (repo / "sdk").symlink_to(REPO / "sdk", target_is_directory=True)
        for name in ("build/posix/ci-stage.sh", "build/macos/package-macos.sh", "LICENSE"):
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / name, target)
        fonts = root / "fonts"
        fonts.mkdir()
        for name in ("fixture.ttf", "fonts.conf.template", "NOTICE", "SOURCE.md"):
            (fonts / name).write_text("offline packaging fixture\n")
        (src / "LICENSE").write_text("Chromium license fixture\n")
        (src / "chrome").mkdir()
        (src / "chrome/VERSION").write_text("\n".join(
            f"{key}={value}" for key, value in zip(("MAJOR", "MINOR", "BUILD", "PATCH"), version.split("."))) + "\n")
        report = work / "fingerprint-diagnostics/source-final.json"
        report.parent.mkdir()
        report.write_text(json.dumps(stack.verify(src, repo, platform="macos")))
        (work / "upstream-cache-restore.json").write_text('{"fixture": "retain cache report"}\n')
        fixture = MacOSBundleTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.layout(arch)
        fixture.version.rename(fixture.version.with_name(version))
        current = fixture.framework / "Versions/Current"
        current.unlink()
        current.symlink_to(version)
        plist = fixture.app / "Contents/Info.plist"
        plist.write_bytes(plistlib.dumps({"CFBundleExecutable": "Chromium",
                                        "CFBundleShortVersionString": version}))
        out = src / "out/Chromix"
        out.mkdir(parents=True)
        shutil.copytree(fixture.app, out / "Chromium.app", symlinks=True)
        build = repo / "build/macos/build.sh"
        build.write_text("#!/bin/sh\nexit " + ("9" if damage == "compile" else "0") + "\n")
        build.chmod(0o755)
        if damage == "source":
            (src / "file.cc").write_text("changed since source receipt\n")
        elif damage == "receipt":
            report.write_text('{"status":"failed","errors":["source check failed"]}\n')
        elif damage == "missing_receipt":
            report.unlink()
        elif damage == "version":
            plist = out / "Chromium.app/Contents/Info.plist"
            plist.write_bytes(plistlib.dumps({"CFBundleExecutable": "Chromium",
                                            "CFBundleShortVersionString": "0.0.0.0"}))
        elif damage == "source_version":
            (src / "chrome/VERSION").write_text("MAJOR=156\nMINOR=0\nBUILD=8078\nPATCH=79\n")
        elif damage == "architecture":
            from tools.tests.test_verify_macos_bundle import thin
            (out / "Chromium.app/Contents/MacOS/Chromium").write_bytes(
                thin("arm64" if arch == "x64" else "x64"))
        elif damage == "helper":
            (out / "Chromium.app/Contents/Frameworks/Chromium Framework.framework/Helpers/"
                   "Chromium Helper.app/Contents/MacOS/Chromium Helper").unlink()
        elif damage == "cache":
            (src / ".chromix-upstream-restored.json").write_text('{"fixture":"bad cache receipt"}\n')
        before_receipt = report.read_bytes() if report.exists() else None
        bin_dir = root / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "python3"
        shim.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''\
            import json, os, sys, time, zipfile
            from pathlib import Path
            args = sys.argv[1:]
            name = Path(args[0]).name if args else ""
            failure = os.environ["FIXTURE_FAILURE"]
            root = Path(os.environ["FIXTURE_ROOT"])
            def value(flag):
                return Path(args[args.index(flag) + 1])
            if name == "platform_pins.py":
                print(os.environ["FIXTURE_VERSION"])
                sys.exit(0)
            if name.startswith("validate_macos156_"):
                sys.exit(0)
            if name == "restore_upstream_cache.py":
                sys.exit(31)
            if args[:2] == ["-m", "pip"]:
                sys.exit(23 if failure == "dependencies" else 0)
            if name == "macos_browser_smoke.py":
                output = value("--output")
                output.mkdir()
                report = {"passed": failure != "smoke", "errors": ["native smoke failed"] if failure == "smoke" else []}
                if failure == "runtime_version":
                    report.update(passed=False, version={"status": "version_mismatch"})
                (output / "report.json").write_text(json.dumps(report))
                sys.exit(7 if failure in ("smoke", "runtime_version") else 0)
            if name == "fingerprint_acceptance.py":
                output = value("--output-dir")
                output.mkdir()
                status = "incomplete" if failure in ("incomplete", "incomplete_zero") else "failed" if failure in ("fingerprint", "source_error") else "passed"
                errors = ["source changed since its freshness check: file.cc"] if failure == "source_error" else ["device: real fixture failure"] if failure == "fingerprint" else []
                report = {"status": status, "ci_gate_passed": status == "passed", "errors": errors,
                          "gaps": ["native probe incomplete"] if status == "incomplete" else []}
                if failure in ("startup_missing_version", "observed_wrong_version", "observed_wrong_sha", "preflight_wrong_version"):
                    import hashlib
                    sys.path.insert(0, str(root / "repo/tools"))
                    from fingerprint_acceptance import assess_suite
                    observed = {"status": "failed", "errors": ["native startup failed"],
                                "browser_sha256": hashlib.sha256(value("--browser").read_bytes()).hexdigest()}
                    if failure == "observed_wrong_version":
                        observed["browser_version"] = "0.0.0.0"
                    if failure == "observed_wrong_sha":
                        observed["browser_sha256"] = "0" * 64
                    errors, gaps = assess_suite("device", observed, args[args.index("--expected-sha256") + 1],
                                                args[args.index("--expected-version") + 1])
                    report.update(status="failed", ci_gate_passed=False, errors=errors, gaps=gaps,
                                  suites=[{"name": "device", "report": "device.json"}])
                    (output / "device.json").write_text(json.dumps(observed))
                    if failure == "preflight_wrong_version":
                        (output / "browser.json").write_text(json.dumps({"version": "0.0.0.0"}))
                if failure not in ("missing_report", "timeout"):
                    raw = json.dumps(report, indent=3) + "\\n"
                    (output / "acceptance.json").write_text(raw)
                    (root / "original-acceptance.json").write_text(raw)
                if failure == "source_after":
                    value("--source-root").joinpath("file.cc").write_text("changed by probe")
                if failure == "archive_after":
                    archive = next((root / "work/dist").glob("*.zip"))
                    with archive.open("ab") as stream:
                        stream.write(b"changed after integrity check")
                if failure == "timeout":
                    time.sleep(60)
                sys.exit(17 if report["status"] == "failed" or failure in ("incomplete", "missing_report") else 0)
            if name == "verify_macos_bundle.py" and "--zip" in args and os.environ["FIXTURE_DAMAGE"] in ("crc", "unsafe", "sha"):
                archive = value("--zip")
                # Damage after the packager has verified it, before stage verification.
                marker = root / "zip-verified-once"
                if marker.exists():
                    if os.environ["FIXTURE_DAMAGE"] == "sha":
                        archive.with_name("SHA256SUMS").write_text("0" * 64 + "  " + archive.name + "\\n")
                    elif os.environ["FIXTURE_DAMAGE"] == "unsafe":
                        with zipfile.ZipFile(archive, "a") as stream:
                            stream.writestr("../escape", "invalid")
                    else:
                        import struct
                        with zipfile.ZipFile(archive) as stream:
                            entry = next(e for e in stream.infolist() if e.file_size > 0 and not e.is_dir())
                        data = bytearray(archive.read_bytes())
                        offset = entry.header_offset
                        name_size, extra_size = struct.unpack_from("<HH", data, offset + 26)
                        data[offset + 30 + name_size + extra_size] ^= 1
                        archive.write_bytes(data)
                marker.touch()
            os.execv(sys.executable, [sys.executable, "-B", *args])
            '''))
        shim.chmod(0o755)
        timeout = bin_dir / "timeout"
        timeout.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''\
            import os, sys
            args = sys.argv[1:]
            while args[0] in ("-k", "-s"):
                del args[:2]
            del args[0]
            duration = "1s" if any("fingerprint_acceptance.py" in arg for arg in args) else "30s"
            os.execv(os.environ["REAL_TIMEOUT"], [os.environ["REAL_TIMEOUT"], "-k", "1s", duration, *args])
            '''))
        timeout.chmod(0o755)
        output = root / "github-output"
        env = {key: value for key, value in os.environ.items() if not key.startswith("CHROMIX_")}
        env.update(PATH=str(bin_dir) + os.pathsep + env["PATH"], GITHUB_OUTPUT=str(output),
                   FIXTURE_ROOT=str(root), FIXTURE_VERSION=version, FIXTURE_FAILURE=failure,
                   FIXTURE_DAMAGE=damage or "", CHROMIX_FONTS_DIR=str(fonts),
                   REAL_TIMEOUT=shutil.which("timeout"), CHROMIX_MACOS_COMPILE_RELEASE=str(int(opt_in)),
                   PYTHONDONTWRITEBYTECODE="1")
        if frozen:
            env["CHROMIX_UNCHANGED_RESUME"] = "mac156-x64-stage3" if arch == "x64" else "mac156-arm64-stage8"
        result = subprocess.run(["/bin/bash", str(repo / "build/posix/ci-stage.sh"),
                                 "--platform", "macos", "--arch", arch, "--workdir", str(work)],
                                env=env, capture_output=True, text=True, timeout=45)
        outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
        receipts = list(work.glob("fingerprint-diagnostics/compile-release-*/diagnostics.json"))
        summary = json.loads(receipts[0].read_text()) if receipts else None
        self.assertEqual(report.read_bytes() if report.exists() else None, before_receipt)
        self.assertEqual((work / "upstream-cache-restore.json").read_text(), '{"fixture": "retain cache report"}\n')
        if (root / "original-acceptance.json").exists():
            acceptance = next(work.glob("fingerprint-diagnostics/**/acceptance.json"))
            self.assertEqual(acceptance.read_bytes(), (root / "original-acceptance.json").read_bytes())
        return root, result, outputs, summary

    def assert_final_upload(self, root, result, outputs):
        import yaml
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(outputs["finished"], "true")
        self.assertEqual(outputs["status"], "compiled")
        self.assertEqual(outputs["release_policy"], "macos-compile-release")
        jobs = yaml.safe_load((REPO / ".github/workflows/build-posix-github.yml").read_text())["jobs"]
        for stage in range(1, 9):
            upload = next(s for s in jobs[f"posix-{stage}"]["steps"] if s.get("name") == "Upload final bundle (compile-release)")
            self.assertIn("steps.stage.outputs.release_policy == 'macos-compile-release'", upload["if"])
            steps = jobs[f"posix-{stage}"]["steps"]
            source_upload = next(s for s in steps if s.get("name") == "Upload fingerprint source receipt (compile-release)")
            self.assertEqual(source_upload["if"], upload["if"])
            diagnostics = next(s for s in steps if s.get("name") == "Upload build diagnostics (compile-release)")
            self.assertIn("!cancelled()", diagnostics["if"])
            self.assertIn("/fingerprint-diagnostics/", diagnostics["with"]["path"])
            # Execute a local upload double only when the real workflow condition is true.
            destination = root / f"uploaded-s{stage}"
            command = 'test "$FINISHED" = true && mkdir "$DEST" && cp "$DIST"/*.zip "$DIST/SHA256SUMS" "$DEST/"'
            sent = subprocess.run(["/bin/bash", "-euc", command], capture_output=True,
                                  env={**os.environ, "FINISHED": outputs["finished"],
                                       "DIST": str(root / "work/dist"), "DEST": str(destination)})
            self.assertEqual(sent.returncode, 0, sent.stderr)
            for path in (root / "work/dist").glob("*.zip"):
                self.assertEqual(path.read_bytes(), (destination / path.name).read_bytes())

    def test_native_failure_uploads_compiled_bundle_with_truthful_status_both_architectures(self):
        for arch in ("x64", "arm64"):
            for failure in ("fingerprint", "smoke", "incomplete", "timeout", "dependencies", "missing_report", "incomplete_zero", "none"):
                with self.subTest(arch=arch, failure=failure):
                    root, result, outputs, summary = self.fixture(arch=arch, failure=failure)
                    self.assert_final_upload(root, result, outputs)
                    expected = "passed" if failure == "none" else "incomplete" if failure == "incomplete_zero" else "failed"
                    self.assertEqual(summary["status"], expected)
                    self.assertEqual(outputs["runtime_verified"], "true" if failure == "none" else "false")
                    if failure == "incomplete":
                        self.assertEqual(summary["fingerprint_status"], "incomplete")
                    if failure == "timeout":
                        self.assertEqual(summary["fingerprint_exit_code"], 124)

    def test_integrity_and_build_failures_remain_blocking(self):
        for damage in ("compile", "cache", "source", "receipt", "missing_receipt", "version",
                       "source_version", "architecture", "helper", "crc", "unsafe", "sha"):
            with self.subTest(damage=damage):
                _, result, outputs, _ = self.fixture(damage=damage)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(outputs["finished"], "false")
                self.assertNotIn("release_policy", outputs)
        for failure in ("source_after", "source_error", "archive_after", "runtime_version"):
            with self.subTest(failure=failure):
                _, result, outputs, _ = self.fixture(failure=failure)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(outputs["finished"], "false")

    def test_explicit_opt_in_and_frozen_history_scope(self):
        for version in ("156.0.8078.79", "158.0.8200.1"):
            for opt_in in (False, True):
                with self.subTest(version=version, opt_in=opt_in):
                    root, result, outputs, _ = self.fixture(version=version, opt_in=opt_in)
                    if opt_in:
                        self.assert_final_upload(root, result, outputs)
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(outputs["finished"], "false")
        for arch in ("x64", "arm64"):
            with self.subTest(frozen=arch):
                _, result, outputs, _ = self.fixture(arch=arch, version="156.0.8078.79", opt_in=True, frozen=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(outputs["finished"], "false")
                self.assertIn("fingerprint regression gate failed", result.stderr)

    def test_startup_missing_identity_is_diagnostic_but_observed_mismatches_block(self):
        for arch in ("x64", "arm64"):
            for failure in ("startup_missing_version", "observed_wrong_version", "observed_wrong_sha", "preflight_wrong_version"):
                with self.subTest(arch=arch, failure=failure):
                    root, result, outputs, summary = self.fixture(arch=arch, failure=failure)
                    self.assertEqual(summary["status"], "failed")
                    if failure == "startup_missing_version":
                        self.assert_final_upload(root, result, outputs)
                        self.assertEqual(outputs["runtime_verified"], "false")
                        report = json.loads((root / "original-acceptance.json").read_text())
                        self.assertIn("suite used a different browser version", report["errors"])
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(outputs["finished"], "false")
                        self.assertIn("observed browser identity mismatch", result.stderr)

    def test_generated_workflow_scopes_opt_in_without_continue_on_error(self):
        import yaml
        path = REPO / ".github/workflows/build-posix-github.yml"
        before = path.read_bytes()
        subprocess.run([sys.executable, "tools/gen_posix_workflow.py"], cwd=REPO, check=True, capture_output=True)
        self.assertEqual(before, path.read_bytes())
        workflow = yaml.safe_load(before)
        inputs = workflow.get("on", workflow.get(True))["workflow_call"]["inputs"]
        self.assertEqual(inputs["macos_compile_release"]["default"], False)
        self.assertEqual(inputs["macos_compile_release"]["type"], "boolean")
        for number in range(1, 9):
            stage = next(s for s in workflow["jobs"][f"posix-{number}"]["steps"] if s.get("id") == "stage")
            self.assertNotIn("continue-on-error", stage)
            self.assertEqual(stage["env"]["CHROMIX_MACOS_COMPILE_RELEASE"],
                             "${{ inputs.platform == 'macos' && inputs.macos_compile_release && '1' || '0' }}")


class CompileReleaseWorkflowExecutionTest(unittest.TestCase):
    """Execute generated post-stage run blocks with offline upload/snapshot doubles."""

    def execute(self, stage, platform, policy, finished, fault, cancelled=False):
        import re
        import yaml
        workflow = yaml.safe_load((REPO / ".github/workflows/build-posix-github.yml").read_text())
        steps = workflow["jobs"][f"posix-{stage}"]["steps"]
        steps = steps[next(i for i, step in enumerate(steps) if step.get("id") == "stage") + 1:]
        with tempfile.TemporaryDirectory(prefix="workflow uploads ") as temp:
            root = Path(temp).resolve()
            runner = root / "runner"
            work = runner / "chromix-build"
            dist = work / "dist"
            dist.mkdir(parents=True)
            (work / "src").mkdir()
            (runner / "chromix-logs").mkdir()
            evidence = work / "fingerprint-diagnostics"
            evidence.mkdir()
            (evidence / "source-final.json").write_text('{"status":"verified"}\n')
            (evidence / "acceptance.json").write_text('{"status":"failed","errors":["native startup failed"]}\n')
            original = (evidence / "acceptance.json").read_bytes()
            asset = "chromix-mac-x64" if platform == "macos" else "chromix-linux-x64"
            (dist / (asset + ".zip")).write_bytes(b"integrity-verified fixture")
            (dist / "SHA256SUMS").write_text("fixture checksum\n")
            tools = root / "tools"
            tools.mkdir()
            parts = root / "build/posix/ci-parts.sh"
            parts.parent.mkdir(parents=True)
            parts.write_text('#!/bin/bash\nset -eu\n[ "$FAULT" != snapshot ] || exit 23\n'
                             'mkdir -p "$2/p1"\nprintf "snapshot" > "$2/p1/volume"\n')
            (tools / "snapshot_volumes.py").write_text(
                'import sys\nfrom pathlib import Path\nsys.stdout.buffer.write(str(Path(sys.argv[1])/"p1/volume").encode()+b"\\0")\n')
            (tools / "verify_snapshot_tar.py").write_text(
                'import os,sys\nsys.stdin.buffer.read()\nsys.exit(29 if os.environ["FAULT"] == "verify" else 0)\n')
            (tools / "verify_linux_checkpoint.py").write_text('print("{}")\n')
            bin_dir = root / "bin"
            bin_dir.mkdir()
            (bin_dir / "zstd").write_text('#!/bin/sh\ncat\n')
            (bin_dir / "zstd").chmod(0o755)
            (bin_dir / "python3").symlink_to(sys.executable)
            # An unfinished stage supplies its own checkpoint instead of runtime_checkpoint.
            if not finished:
                snap = work / f".snapshot-stage-{stage}/p1"
                snap.mkdir(parents=True)
                (snap / "volume").write_text("handoff")
            state = {"inputs.platform": platform, "inputs.arch": "x64", "inputs.artifact": asset,
                     "inputs.resume_migration": "", "runner.temp": str(runner), "github.run_attempt": "1",
                     "steps.stage.outcome": "success", "steps.stage.outputs.finished": str(finished).lower(),
                     "steps.stage.outputs.release_policy": policy,
                     "steps.stage.outputs.compiled_ready": "true" if finished else "",
                     "steps.stage.outputs.upload_snapshot": "" if finished else "true"}
            failed = False
            trace, failures, uploaded = [], [], []

            def evaluate(expression, implicit=False):
                expression = expression.removeprefix("${{ ").removesuffix(" }}")
                if implicit and not any(token in expression for token in ("success()", "failure()", "always()", "cancelled()")):
                    if failed or cancelled:
                        return False
                expression = expression.replace("!cancelled()", str(not cancelled)).replace("cancelled()", str(cancelled))
                expression = expression.replace("always()", "True").replace("success()", str(not failed and not cancelled))
                expression = expression.replace("failure()", str(failed))
                expression = re.sub(r"\b(?:inputs|steps|runner|github)\.[\w.-]+", lambda m: repr(state.get(m[0], "")), expression)
                return eval(re.sub(r"!(?!=)", " not ", expression).replace("&&", " and ").replace("||", " or "),
                            {"__builtins__": {}})

            def render(text):
                return re.sub(r"\$\{\{ (.*?) \}\}", lambda m: str(evaluate(m[1])), text)

            for step in steps:
                if not evaluate(step.get("if", "success()"), implicit=True):
                    continue
                name = step["name"]
                trace.append(name)
                output = root / "step-output"
                output.write_text("")
                env = {**os.environ, "RUNNER_TEMP": str(runner), "GITHUB_OUTPUT": str(output), "FAULT": fault,
                       "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
                if "run" in step:
                    result = subprocess.run(["/bin/bash", "-e", "-o", "pipefail", "-c", render(step["run"])],
                                            cwd=root, env=env, capture_output=True, timeout=10)
                else:
                    artifact = render(step["with"]["name"])
                    destination = root / "uploaded" / artifact
                    is_bundle = name.startswith("Upload final bundle")
                    is_receipt = name.startswith("Upload fingerprint source receipt")
                    is_logs = name.startswith("Upload build diagnostics")
                    should_fail = (fault == "diagnostics" and is_logs or fault == "tree" and name.startswith("Upload tree")
                                   or fault == "bundle" and is_bundle or fault == "receipt" and is_receipt)
                    paths = [Path(render(line.strip())) for line in step["with"]["path"].splitlines() if line.strip()]
                    if is_bundle or is_receipt or is_logs:
                        # The action double copies the paths from the generated workflow.
                        paths = [p for p in paths if p.exists()]
                        command = 'exit 31' if should_fail else 'mkdir -p "$1"; shift; cp -R "$@" "$DEST/"'
                        result = subprocess.run(["/bin/bash", "-euc", command, "upload", str(destination), *map(str, paths)],
                                                env={**env, "DEST": str(destination)}, capture_output=True, timeout=10)
                        if result.returncode == 0:
                            uploaded.append(artifact)
                    else:
                        result = subprocess.run(["/bin/bash", "-c", "exit 31" if should_fail else "exit 0"], timeout=10)
                outcome = "failure" if result.returncode else "success"
                if step.get("id"):
                    state[f"steps.{step['id']}.outcome"] = outcome
                    for line in output.read_text().splitlines():
                        key, value = line.split("=", 1)
                        state[f"steps.{step['id']}.outputs.{key}"] = value
                if result.returncode:
                    failures.append(name)
                    continued = step.get("continue-on-error", False)
                    if isinstance(continued, str):
                        continued = evaluate(continued)
                    if not continued:
                        failed = True
            self.assertEqual((evidence / "acceptance.json").read_bytes(), original)
            if asset in uploaded:
                self.assertEqual((root / "uploaded" / asset / (asset + ".zip")).read_bytes(), b"integrity-verified fixture")
            logs = f"{asset}-logs-s{stage}-attempt-1"
            if logs in uploaded:
                self.assertEqual((root / "uploaded" / logs / "fingerprint-diagnostics/acceptance.json").read_bytes(), original)
            return failed, trace, failures, uploaded

    def test_auxiliary_failures_do_not_prevent_compiled_uploads_or_fail_the_job(self):
        for stage in (1, 8):
            for fault in ("snapshot", "verify", "tree", "diagnostics"):
                with self.subTest(stage=stage, fault=fault):
                    failed, trace, failures, uploaded = self.execute(stage, "macos", "macos-compile-release", True, fault)
                    self.assertFalse(failed)
                    failed_step = {"snapshot": "Preserve latest macOS compiled checkpoint",
                                   "verify": "Verify handoff snapshot", "tree": "Upload tree part 1",
                                   "diagnostics": "Upload build diagnostics (compile-release)"}[fault]
                    self.assertIn(failed_step, failures)
                    self.assertIn("chromix-mac-x64", uploaded)
                    self.assertIn("chromix-mac-x64-fingerprint-source", uploaded)
                    self.assertLess(trace.index("Upload fingerprint source receipt (compile-release)"),
                                    trace.index("Preserve latest macOS compiled checkpoint"))
                    if fault != "diagnostics":
                        self.assertIn(f"chromix-mac-x64-logs-s{stage}-attempt-1", uploaded)

    def test_release_upload_failure_is_fatal_and_other_upload_still_attempted(self):
        for fault in ("bundle", "receipt"):
            with self.subTest(fault=fault):
                failed, trace, _, _ = self.execute(1, "macos", "macos-compile-release", True, fault)
                self.assertTrue(failed)
                self.assertIn("Upload final bundle (compile-release)", trace)
                self.assertIn("Upload fingerprint source receipt (compile-release)", trace)

    def test_unfinished_and_legacy_failures_stay_blocking(self):
        for platform in ("macos", "linux"):
            with self.subTest(platform=platform):
                failed, trace, _, uploaded = self.execute(1, platform, "", False, "verify")
                self.assertTrue(failed)
                self.assertFalse(any("final bundle" in name for name in trace))
                self.assertFalse(any(name.startswith("Upload tree part") for name in trace))
        failed, trace, _, uploaded = self.execute(1, "macos", "", True, "snapshot")
        self.assertTrue(failed)
        self.assertNotIn("Upload final bundle", trace)
        failed, trace, _, uploaded = self.execute(1, "linux", "", True, "diagnostics")
        self.assertTrue(failed)
        self.assertIn("Upload final bundle", trace)
        self.assertNotIn("Upload fingerprint source receipt", trace)
        self.assertFalse(any("compile-release" in name for name in trace))


class PosixChecksumContractTest(unittest.TestCase):
    def test_exact_single_target_and_digest_on_both_platforms(self):
        source = (REPO / "build/posix/ci-stage.sh").read_text()
        checksum = source.split('cd "$DEST_DIST"\n', 1)[1].split('\nSMOKE_DIR=', 1)[0]
        for platform in ("linux", "macos"):
            for arch in ("x64", "arm64"):
                with tempfile.TemporaryDirectory(prefix="checksum contract ") as temp:
                    root = Path(temp)
                    asset = f"chromix-{'mac' if platform == 'macos' else 'linux'}-{arch}.zip"
                    payload = b"target archive bytes"
                    (root / asset).write_bytes(payload)
                    digest = hashlib.sha256(payload).hexdigest()
                    valid = f"{digest}  {asset}\n"
                    other = asset.replace(".zip", "Xzip")
                    (root / other).write_bytes(payload)
                    cases = {
                        "valid": (valid, True),
                        "wrong_name": (f"{digest}  {other}\n", False),
                        "path_alias": (f"{digest}  ./{asset}\n", False),
                        "duplicate": (valid + valid, False),
                        "extra_valid_file": (valid + f"{digest}  {other}\n", False),
                        "extra_malformed": (valid + "invalid\n", False),
                        "blank_line": (valid + "\n", False),
                        "wrong_digest": ("0" * 64 + f"  {asset}\n", False),
                        "short_digest": (digest[:-1] + f"  {asset}\n", False),
                        "invalid_digest": ("g" * 64 + f"  {asset}\n", False),
                        "trailing_space": (valid.rstrip("\n") + " \n", False),
                        "empty": ("", False),
                    }
                    for name, (manifest, passes) in cases.items():
                        with self.subTest(platform=platform, arch=arch, case=name):
                            (root / "SHA256SUMS").write_text(manifest)
                            script = 'set -euo pipefail\ndie() { echo "$*" >&2; exit 1; }\n' + checksum
                            result = subprocess.run(["/bin/bash", "-c", script], cwd=root,
                                                    env={**os.environ, "PLATFORM": platform, "ARCH": arch},
                                                    capture_output=True, text=True, timeout=10)
                            self.assertEqual(result.returncode == 0, passes, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
