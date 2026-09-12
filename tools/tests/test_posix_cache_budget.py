"""Exercise optional-cache deadlines without network access or compilation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

try:
    import fcntl
except ImportError:
    raise unittest.SkipTest('POSIX flock/process-group tests require fcntl')

REPO = Path(__file__).resolve().parents[2]
BASH32 = Path.home() / ".local/bash-3.2-for-ci/bash"
TIMEOUT = shutil.which("timeout") or shutil.which("gtimeout")
FAKE_FETCHER = '''import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--platform", required=True, choices=("linux", "macos"))
parser.add_argument("--arch", required=True, choices=("x64", "arm64"))
parser.add_argument("--destination", required=True)
parser.add_argument("--run-id")
args = parser.parse_args()
Path(os.environ["TEST_ARGS"]).write_text(json.dumps(sys.argv[1:]))
mode = os.environ["TEST_MODE"]
if mode == "fatal":
    sys.exit(7)
if mode == "miss":
    destination = Path(args.destination)
    destination.mkdir(parents=True)
    (destination / "result.json").write_text(json.dumps({
        "owner": "chromix-upstream-cache-v1", "destination": str(destination.absolute()),
        "status": "miss", "reason": "unavailable", "source": None}))
    sys.exit(0)
if mode == "child":
    child = subprocess.Popen([sys.executable, "-c", """
import fcntl, signal, sys, time
stream = open(sys.argv[1], "r+")
fcntl.flock(stream, fcntl.LOCK_EX)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("ready", flush=True)
time.sleep(60)
""", str(Path(args.destination) / ".lock")], stdout=subprocess.PIPE)
    assert child.stdout.readline() == b"ready\\n"
time.sleep(60)
'''


@unittest.skipUnless(TIMEOUT, "GNU timeout or gtimeout required")
class PosixCacheBudgetTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cache budget ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.script = self.repo / "build/posix/fetch-upstream-cache.sh"
        self.script.parent.mkdir(parents=True)
        shutil.copy2(REPO / "build/posix/fetch-upstream-cache.sh", self.script)
        (self.repo / "tools").mkdir()
        (self.repo / "tools/fetch_upstream_cache.py").write_text(FAKE_FETCHER)
        self.destination = self.root / "cache with spaces"
        self.args = ["--platform", "linux", "--arch", "x64",
                     "--destination", str(self.destination)]
        self.log = self.root / "args.json"
        self.env = {**os.environ, "CHROMIX_CACHE_TIMEOUT_SECONDS": "1",
                    "TEST_ARGS": str(self.log), "TEST_MODE": "sleep"}

    def run_wrapper(self, *, bash=None, args=None, mode="sleep", env=None):
        return subprocess.run(
            [str(bash or shutil.which("bash")), str(self.script),
             *(self.args if args is None else args)],
            cwd=self.root, env={**self.env, "TEST_MODE": mode, **(env or {})},
            capture_output=True, text=True, timeout=10)

    def seed(self):
        self.destination.mkdir()
        self.metadata = {
            "owner": "chromix-upstream-cache-v1", "destination": str(self.destination),
            "status": "hit", "source": str(self.destination / "tree/src"),
            "manifest": {"sha256": "fixture", "run_id": 42}, "download_bytes": 123,
        }
        (self.destination / "result.json").write_text(json.dumps(self.metadata))
        (self.destination / "tree/src").mkdir(parents=True)
        (self.destination / "tree/src/partial").write_text("partial payload")
        for name in (".inner", ".download.zip", ".result.tmp", ".lock"):
            (self.destination / name).write_text("partial")

    def assert_timeout_miss(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected = {**self.metadata, "status": "miss", "reason": "cache_timeout", "source": None}
        self.assertEqual(json.loads((self.destination / "result.json").read_text()), expected)
        self.assertEqual(json.loads(result.stdout), expected)
        self.assertEqual({path.name for path in self.destination.iterdir()}, {"result.json"})
        self.assertIn("cache_timeout", result.stderr)

    def snapshot(self):
        return {str(path.relative_to(self.destination)):
                ("link", os.readlink(path)) if path.is_symlink() else
                ("directory", None) if path.is_dir() else ("file", path.read_bytes())
                for path in self.destination.rglob("*")}

    def test_timeout_cleans_payload_preserves_metadata_and_does_not_follow_tree_links(self):
        self.seed()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("untouched")
        (self.destination / "tree/link").symlink_to(outside, target_is_directory=True)
        self.assert_timeout_miss(self.run_wrapper())
        self.assertEqual((outside / "keep").read_text(), "untouched")
        self.assertEqual(json.loads(self.log.read_text()), self.args)

    def test_timeout_kills_term_resistant_child_before_unlock(self):
        self.seed()
        with (self.destination / ".lock").open("r+") as lock:
            self.assert_timeout_miss(self.run_wrapper(mode="child"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_normal_miss_exits_zero_and_preserves_reason(self):
        result = self.run_wrapper(mode="miss")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.log.read_text()), self.args)
        self.assertEqual(json.loads((self.destination / "result.json").read_text())["reason"],
                         "unavailable")

    def test_fatal_arguments_and_helper_errors_are_not_misses(self):
        for args, mode, status in ((self.args + ["--invalid"], "miss", 2),
                                   (self.args[:-1], "miss", 2),
                                   ([], "miss", 2), (self.args, "fatal", 7)):
            with self.subTest(args=args, mode=mode):
                result = self.run_wrapper(args=args, mode=mode)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertNotIn("cache_timeout", result.stderr)
                self.assertFalse(self.destination.exists())

    def test_timeout_refuses_unowned_malformed_or_unexpected_contents_without_changes(self):
        for problem in ("owner", "destination", "relative", "malformed", "array", "missing",
                        "unexpected", "result_link", "payload_link", "wrong_type", "lock_directory"):
            with self.subTest(problem=problem):
                self.seed()
                result_path = self.destination / "result.json"
                outside = self.root / "outside"
                outside.write_text("untouched")
                if problem in ("owner", "destination", "relative"):
                    key = "owner" if problem == "owner" else "destination"
                    self.metadata[key] = "someone-else" if problem == "owner" else (
                        str(self.destination) + "/" if problem == "destination" else self.destination.name)
                    result_path.write_text(json.dumps(self.metadata))
                elif problem in ("malformed", "array"):
                    result_path.write_text("{" if problem == "malformed" else "[]")
                elif problem == "missing":
                    result_path.unlink()
                elif problem == "unexpected":
                    (self.destination / "user-file").write_text("do not delete")
                elif problem in ("result_link", "payload_link"):
                    path = result_path if problem == "result_link" else self.destination / ".inner"
                    path.unlink()
                    path.symlink_to(outside)
                elif problem == "wrong_type":
                    shutil.rmtree(self.destination / "tree")
                    (self.destination / "tree").write_text("not a tree")
                else:
                    (self.destination / ".lock").unlink()
                    (self.destination / ".lock").mkdir()
                before = self.snapshot()
                result = self.run_wrapper()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("cleanup refused", result.stderr)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(outside.read_text(), "untouched")
                shutil.rmtree(self.destination)

    def test_timeout_refuses_symlinked_destination_or_parent(self):
        self.seed()
        before = self.snapshot()
        for target in (self.destination, self.root):
            with self.subTest(target=target):
                link = self.root / "alias"
                link.symlink_to(target, target_is_directory=True)
                path = link if target == self.destination else link / self.destination.name
                result = self.run_wrapper(args=self.args[:-1] + [str(path)])
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("cleanup refused", result.stderr)
                self.assertEqual(self.snapshot(), before)
                link.unlink()

    def test_positive_integer_budget_required(self):
        for value in ("", "0", "-1", "1.5", "1s", "01", "1\n", "abc"):
            with self.subTest(value=value):
                result = self.run_wrapper(env={"CHROMIX_CACHE_TIMEOUT_SECONDS": value})
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("positive integer", result.stderr)
                self.assertFalse(self.log.exists())

    def host_path(self, timeout_name=None, python=True):
        tools = self.root / "bin"
        tools.mkdir(exist_ok=True)
        for child in tools.iterdir():
            child.unlink()
        (tools / "dirname").symlink_to(shutil.which("dirname"))
        if python:
            (tools / "python3").symlink_to(shutil.which("python3"))
        if timeout_name:
            (tools / timeout_name).symlink_to(TIMEOUT)
        return str(tools)

    def test_gtimeout_fallback(self):
        self.seed()
        self.assert_timeout_miss(self.run_wrapper(env={"PATH": self.host_path("gtimeout")}))

    def test_missing_executables_fail(self):
        for timeout_name, python, message in ((None, True, "timeout or gtimeout"),
                                              ("timeout", False, "python3")):
            with self.subTest(message=message):
                result = self.run_wrapper(env={"PATH": self.host_path(timeout_name, python)})
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertFalse(self.log.exists())

    def test_default_budget_and_kill_grace(self):
        tools = Path(self.host_path())
        timeout = tools / "timeout"
        timeout.write_text(f"#!{shutil.which('python3')}\nimport os, sys\n"
                           "assert sys.argv[1:4] == ['-k', '30s', '3600s'], sys.argv\n"
                           "os.execv(sys.argv[4], sys.argv[4:])\n")
        timeout.chmod(0o755)
        self.env.pop("CHROMIX_CACHE_TIMEOUT_SECONDS")
        result = self.run_wrapper(mode="miss", env={"PATH": str(tools)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.log.exists())

    @unittest.skipUnless(BASH32.exists(), "Bash 3.2 required")
    def test_bash32_parse_and_run(self):
        result = subprocess.run([str(BASH32), "-n", str(self.script)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        for args in ([], self.args[:-1], self.args + ["--invalid"]):
            result = self.run_wrapper(bash=BASH32, args=args, mode="miss")
            self.assertEqual(result.returncode, 2, result.stderr)
        result = self.run_wrapper(bash=BASH32, mode="miss")
        self.assertEqual(result.returncode, 0, result.stderr)
        shutil.rmtree(self.destination)
        self.seed()
        self.assert_timeout_miss(self.run_wrapper(bash=BASH32))


if __name__ == "__main__":
    unittest.main()
