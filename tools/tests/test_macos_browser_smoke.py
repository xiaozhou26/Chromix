"""Subprocess fixtures only; no browser, build, downloads, or native tooling required."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tools import macos_browser_smoke as smoke

VERSION = "152.0.7977.82"


@unittest.skipUnless(os.name == "posix", "process groups require POSIX")
class MacOSBrowserSmokeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mac smoke ' ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "evidence"
        self.profile = self.root / "profile"
        self.launcher = self.root / "bundle/launcher"
        self.launcher.parent.mkdir()
        self.processes = []
        original = subprocess.Popen

        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            self.processes.append(process)
            return process

        patcher = mock.patch.object(smoke.subprocess, "Popen", side_effect=launch)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for process in self.processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

    def browser(self, *, version=VERSION, body=None, version_exit=0):
        if body is None:
            body = f"print({smoke.DOM_MARKER!r})"
        self.launcher.write_text(
            f"#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n"
            "if '--version' in sys.argv:\n"
            f"    print('Chromium ' + {version!r})\n    sys.exit({version_exit})\n"
            "profile = Path(next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--user-data-dir=')))\n"
            "(profile / 'cache').write_text('temporary data')\n" + body + "\n")
        self.launcher.chmod(0o755)

    def run_fixture(self, source, *, timeout=0.25, limit=smoke.MAX_OUTPUT_BYTES, **kwargs):
        self.output.mkdir(exist_ok=True)
        result = smoke.run_command(
            [sys.executable, "-c", source], self.output, "fixture", timeout=timeout,
            max_output_bytes=limit, **kwargs)
        self.assertEqual(json.loads((self.output / "fixture.json").read_text()), result)
        self.assertLessEqual(sum((self.output / f"fixture.{s}.log").stat().st_size
                                 for s in ("stdout", "stderr")), limit)
        self.assert_closed()
        return result

    def assert_closed(self):
        for process in self.processes:
            self.assertIsNotNone(process.poll())
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)

    def run_browser(self, **kwargs):
        report = smoke.run_smoke(self.launcher, self.output, self.profile, VERSION, **kwargs)
        self.assertEqual(report, json.loads((self.output / "report.json").read_text()))
        for name in ("version", "headless"):
            self.assertEqual(report[name], json.loads((self.output / f"{name}.json").read_text()))
        self.assertTrue(report["profile_cleaned"])
        self.assertFalse(self.profile.exists())
        self.assert_closed()
        return report

    def test_success_preserves_caller_cwd_exact_profile_and_original_flags(self):
        self.browser(body="import json\nprint(json.dumps({'cwd': os.getcwd(), 'argv': sys.argv[1:]}))\n"
                     f"print({smoke.DOM_MARKER!r})\nprint('diagnostic', file=sys.stderr)")
        self.output.mkdir()
        cwd = Path.cwd()
        profile_argument = os.path.relpath(self.profile, cwd)
        result = smoke.run_smoke(self.launcher, self.output, profile_argument, VERSION)
        self.assertTrue(result["passed"])
        self.assertEqual(result["cwd"], str(cwd))
        evidence = json.loads((self.output / "headless.stdout.log").read_text().splitlines()[0])
        self.assertEqual(evidence["cwd"], str(cwd))
        self.assertEqual(evidence["argv"], [
            "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            f"--user-data-dir={profile_argument}", "--dump-dom", smoke.SMOKE_URL])
        self.assertEqual((result["version"]["timeout_seconds"], result["headless"]["timeout_seconds"]), (30, 60))
        self.assertEqual((self.output / "headless.stderr.log").read_text(), "diagnostic\n")
        self.assertEqual(result["headless"]["returncode"], 0)
        self.assertFalse(self.profile.exists())
        self.assert_closed()

    def test_nonzero_with_valid_marker_remains_failure(self):
        self.browser(body=f"print({smoke.DOM_MARKER!r}); sys.exit(7)")
        result = self.run_browser()
        self.assertFalse(result["passed"])
        self.assertEqual(result["headless"]["status"], "nonzero_exit")
        self.assertEqual(result["headless"]["returncode"], 7)
        self.assertTrue(result["headless"]["marker_present"])
        self.assertIn(smoke.DOM_MARKER, (self.output / "headless.stdout.log").read_text())

    def test_missing_marker_fails(self):
        self.browser(body="print('<p>wrong</p>')")
        result = self.run_browser()
        self.assertFalse(result["passed"])
        self.assertEqual(result["headless"]["status"], "marker_missing")
        self.assertEqual(result["headless"]["returncode"], 0)

    def test_wrong_partial_and_embedded_versions_fail(self):
        for index, version in enumerate(("0.0.0.0", "152.0.7977", "152.0.7977.820",
                                         "1152.0.7977.82", VERSION + ".1", "prefix" + VERSION)):
            with self.subTest(version=version):
                self.output = self.root / f"version-{index}"
                self.browser(version=version)
                result = self.run_browser()
                self.assertFalse(result["passed"])
                self.assertEqual(result["version"]["status"], "version_mismatch")
                self.assertEqual(result["headless"]["status"], "not_run")
                self.assertEqual((self.output / "headless.stdout.log").read_bytes(), b"")

    def test_nonzero_version_with_valid_token_fails(self):
        self.browser(version_exit=9)
        result = self.run_browser()
        self.assertEqual(result["version"]["status"], "nonzero_exit")
        self.assertTrue(result["version"]["version_matches"])
        self.assertEqual(result["version"]["returncode"], 9)
        self.assertFalse(result["passed"])

    def test_timeout_retains_partial_stdout_and_pre_cleanup_returncode(self):
        result = self.run_fixture("import time; print('partial', flush=True); time.sleep(30)")
        self.assertEqual(result["status"], "timeout")
        self.assertIsNone(result["returncode"])
        self.assertEqual(result["returncode_after_cleanup"], -signal.SIGKILL)
        self.assertEqual((self.output / "fixture.stdout.log").read_text(), "partial\n")
        self.assertLess(result["total_elapsed_seconds"], 5)
        self.assertGreaterEqual(result["elapsed_seconds"], 0.2)

    def test_headless_timeout_cleans_exact_profile(self):
        self.browser(body="import time\nprint('partial', flush=True)\ntime.sleep(30)")
        result = self.run_browser(headless_timeout=0.25)
        self.assertFalse(result["passed"])
        self.assertEqual(result["headless"]["status"], "timeout")
        self.assertEqual((self.output / "headless.stdout.log").read_text(), "partial\n")

    def test_inherited_child_pipes_after_parent_exit_are_killed_and_bounded(self):
        child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
        source = ("import subprocess,sys\n"
                  f"p = subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                  "print(p.pid, flush=True)\n")
        calls = []
        original = os.killpg

        def kill(group, sig):
            calls.append((group, sig))
            return original(group, sig)

        with mock.patch.object(smoke.os, "killpg", side_effect=kill):
            result = self.run_fixture(source)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["returncode_after_cleanup"], 0)
        self.assertIn((result["pid"], signal.SIGKILL), calls)
        self.assertLess(result["total_elapsed_seconds"], 5)
        child_pid = int((self.output / "fixture.stdout.log").read_text())
        # A killed orphan can remain a zombie until the host init reaps it.
        if sys.platform.startswith("linux"):
            deadline = time.monotonic() + 1
            state = None
            while time.monotonic() < deadline:
                try:
                    state = Path(f"/proc/{child_pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                except FileNotFoundError:
                    state = "gone"
                if state in ("Z", "gone"):
                    break
                time.sleep(0.01)
            self.assertIn(state, ("Z", "gone"))

    def test_ignored_sigterm_cannot_prevent_cleanup(self):
        result = self.run_fixture(
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(30)")
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["returncode_after_cleanup"], -signal.SIGKILL)
        self.assertLess(result["total_elapsed_seconds"], 5)

    def test_process_wait_is_bounded_even_after_both_pipes_close(self):
        result = self.run_fixture("import os,time; os.close(1); os.close(2); time.sleep(30)")
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["returncode_after_cleanup"], -signal.SIGKILL)

    def test_output_limit_is_combined_and_not_success(self):
        result = self.run_fixture(
            "import os,time; os.write(1,b'x'*200); os.write(2,b'y'*200); time.sleep(30)", limit=256)
        self.assertEqual(result["status"], "output_limit")
        self.assertEqual(result["captured_bytes"], 256)

    def test_signal_exit_retains_raw_returncode(self):
        result = self.run_fixture("import os,signal; os.kill(os.getpid(),signal.SIGTERM)")
        self.assertEqual(result["status"], "nonzero_exit")
        self.assertEqual(result["returncode"], -signal.SIGTERM)

    def test_launch_error_is_recorded_and_profile_cleaned(self):
        result = self.run_browser()
        self.assertFalse(result["passed"])
        self.assertEqual(result["version"]["status"], "launch_error")
        self.assertIsNone(result["version"]["returncode"])
        self.assertEqual(result["headless"]["status"], "not_run")

    def test_existing_evidence_and_profile_are_never_overwritten(self):
        self.browser()
        self.run_browser()
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        with self.assertRaises(FileExistsError):
            smoke.run_smoke(self.launcher, self.output, self.profile, VERSION)
        with self.assertRaises(FileExistsError):
            smoke.run_command([str(self.launcher), "--version"], self.output, "version", timeout=1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})
        self.profile.mkdir()
        (self.profile / "keep").write_text("untouched")
        with self.assertRaises(FileExistsError):
            smoke.run_smoke(self.launcher, self.root / "other", self.profile, VERSION)
        self.assertEqual((self.profile / "keep").read_text(), "untouched")

    def test_output_inside_profile_and_output_symlink_are_rejected(self):
        with self.assertRaises(ValueError):
            smoke.run_smoke(self.launcher, self.profile / "evidence", self.profile, VERSION)
        actual = self.root / "actual"
        actual.mkdir()
        self.output.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(FileExistsError):
            smoke.run_smoke(self.launcher, self.output, self.profile, VERSION)
        self.assertEqual(list(actual.iterdir()), [])

    def test_macos_timeout_diagnostics_precede_kill_and_cannot_change_failure(self):
        observed = []
        original = smoke._macos_timeout_diagnostics

        def diagnostics(pid, output, name, limit):
            observed.append(self.processes[0].poll())
            return original(pid, output, name, limit)

        original_capture = smoke._capture
        diagnostic_calls = []

        def capture(command, **kwargs):
            if command[0] in ("/bin/ps", "/usr/bin/sample"):
                diagnostic_calls.append((command, kwargs))
                if command[0] == "/bin/ps":
                    pid = self.processes[0].pid
                    stdout = f"{pid} 1 {pid} S launcher\n999999 1 999999 S unrelated\n".encode()
                else:
                    stdout = b"sample evidence"
                return {"status": "passed", "returncode": 0, "captured_bytes": len(stdout)}, {
                    "stdout": stdout, "stderr": b""}
            return original_capture(command, **kwargs)

        with mock.patch.object(smoke.sys, "platform", "darwin"), \
                mock.patch.object(smoke, "_capture", side_effect=capture), \
                mock.patch.object(smoke, "_macos_timeout_diagnostics", side_effect=diagnostics), \
                mock.patch.dict(os.environ, {"SECRET_FIXTURE_TOKEN": "do-not-serialize"}):
            result = self.run_fixture("import time; print('partial',flush=True); time.sleep(30)")
        self.assertEqual(observed, [None])
        self.assertEqual(result["status"], "timeout")
        self.assertEqual([call[0][0] for call in diagnostic_calls], ["/bin/ps", "/usr/bin/sample"])
        self.assertEqual(diagnostic_calls[1][0][1], str(result["pid"]))
        self.assertEqual(diagnostic_calls[1][0][-2:], ["-file", "/dev/stdout"])
        for _, kwargs in diagnostic_calls:
            self.assertEqual(kwargs["env"], {"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
            self.assertLessEqual(kwargs["timeout"], 2)
            self.assertLessEqual(kwargs["kill_timeout"], 0.2)
        self.assertNotIn("unrelated", (self.output / "fixture-ps.stdout.log").read_text())
        self.assertLessEqual(sum(p.stat().st_size for p in self.output.glob("*.log")), smoke.MAX_OUTPUT_BYTES)
        for path in self.output.iterdir():
            self.assertNotIn(b"do-not-serialize", path.read_bytes())

    def test_diagnostic_exception_still_kills_browser_and_saves_failure(self):
        with mock.patch.object(smoke, "_macos_timeout_diagnostics", side_effect=OSError("sample failed")):
            result = self.run_fixture("import time; time.sleep(30)")
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["diagnostics"]["status"], "failed")
        self.assertEqual(result["returncode_after_cleanup"], -signal.SIGKILL)

    def test_diagnostic_subprocess_timeout_is_bounded_without_real_mac_tools(self):
        self.output.mkdir()
        original_capture = smoke._capture
        seen = []

        def capture(command, **kwargs):
            seen.append(command[0])
            source = "import sys; sys.exit(2)" if command[0] == "/bin/ps" else (
                "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                "print('partial sample',flush=True); time.sleep(30)")
            return original_capture([sys.executable, "-c", source], **kwargs)

        started = time.monotonic()
        with mock.patch.object(smoke.sys, "platform", "darwin"), \
                mock.patch.object(smoke, "DIAGNOSTIC_TIMEOUT", 0.6), \
                mock.patch.object(smoke, "_capture", side_effect=capture):
            result = smoke._macos_timeout_diagnostics(12345, self.output, "fixture", 1024)
        self.assertEqual(seen, ["/bin/ps", "/usr/bin/sample"])
        self.assertEqual(result["commands"]["ps"]["status"], "nonzero_exit")
        self.assertEqual(result["commands"]["sample"]["status"], "timeout")
        self.assertEqual(result["commands"]["sample"]["returncode_after_cleanup"], -signal.SIGKILL)
        self.assertIn("partial sample", (self.output / "fixture-sample.stdout.log").read_text())
        self.assertLess(time.monotonic() - started, 2)
        self.assert_closed()

    def test_diagnostic_logs_share_remaining_browser_output_budget(self):
        self.output.mkdir()

        def capture(command, **kwargs):
            return {"status": "output_limit", "returncode": None}, {
                "stdout": b"", "stderr": b"x" * kwargs["limit"]}

        with mock.patch.object(smoke.sys, "platform", "darwin"), \
                mock.patch.object(smoke, "_capture", side_effect=capture):
            result = smoke._macos_timeout_diagnostics(12345, self.output, "fixture", 31)
        self.assertEqual(sum(p.stat().st_size for p in self.output.glob("*.log")), 31)
        self.assertEqual(result["commands"]["sample"]["status"], "skipped")

    def test_exact_output_limit_does_not_discard_success(self):
        result = self.run_fixture("import os; os.write(1,b'x'*128)", limit=128)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["captured_bytes"], 128)

    def test_profile_cleanup_failure_cannot_report_success(self):
        self.browser()
        with mock.patch.object(smoke.shutil, "rmtree", side_effect=OSError("cleanup failed")):
            result = smoke.run_smoke(self.launcher, self.output, self.profile, VERSION)
        self.assertFalse(result["passed"])
        self.assertFalse(result["profile_cleaned"])
        self.assertEqual(result["profile_cleanup_error"], "cleanup failed")
        self.assertEqual(result, json.loads((self.output / "report.json").read_text()))
        self.assert_closed()

    def test_cli_has_fixed_budgets_and_returns_failure_without_overwriting(self):
        self.browser()
        args = ["--launcher", str(self.launcher), "--output", str(self.output),
                "--profile", str(self.profile), "--chromium-version", VERSION]
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(smoke.main(args), 0)
        self.assertTrue(json.loads(stdout.getvalue())["passed"])
        with redirect_stderr(io.StringIO()):
            self.assertEqual(smoke.main(args), 1)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            smoke.main(args + ["--timeout", "120"])
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
