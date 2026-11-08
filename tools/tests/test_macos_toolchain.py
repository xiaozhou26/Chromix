"""Exercise macOS SDK selection with fake Xcodes, without building Chromium."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SELECT_XCODE = REPO / "build/macos/select-xcode.sh"
BUILD = REPO / "build/macos/build.sh"
BASH32 = Path.home() / ".local/bash-3.2-for-ci/bash"

MOCK_TOOL = r'''
import json
import os
import sys
from pathlib import Path

config = json.loads(Path(os.environ["MOCK_XCODE_CONFIG"]).read_text())
name = Path(sys.argv[0]).name
args = sys.argv[1:]
developer = os.environ.get("DEVELOPER_DIR") or config.get("current", "")
if developer.rstrip("/").endswith(".app"):
    developer = developer.rstrip("/") + "/Contents/Developer"
with open(os.environ["MOCK_XCODE_LOG"], "a") as log:
    log.write(json.dumps([name, args, developer]) + "\n")
if name == "uname":
    print({"-m": "arm64", "-s": "Darwin"}[args[0]])
    sys.exit(0)
if name == "xcode-select" and args == ["--print-path"]:
    if not developer:
        sys.exit(1)
    print(developer)
    sys.exit(0)
entry = config["xcodes"].get(developer, {})
if name == "xcrun" and args[:2] == ["--sdk", "macosx"]:
    field = {"--show-sdk-version": "sdk", "--show-sdk-path": "path"}.get(args[-1])
    if field and field in entry and not entry.get("fail_" + field):
        print(entry[field])
        sys.exit(0)
if name == "xcodebuild" and args == ["-version"] and entry:
    if not entry.get("fail_xcodebuild"):
        print("Xcode " + entry["xcode"] + "\nBuild version MOCK")
        sys.exit(0)
print("mock tool failed: " + name + " " + " ".join(args), file=sys.stderr)
sys.exit(1)
'''


class MacOSToolchainTest(unittest.TestCase):
    BASH = shutil.which("bash")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromix xcode ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.selector = self.root / "repo/build/macos/select-xcode.sh"
        self.selector.parent.mkdir(parents=True)
        self.selector.write_bytes(SELECT_XCODE.read_bytes())
        self.version = self.root / "repo/CHROMIUM_MACOS_VERSION"
        self.version.write_text("154.0.8037.97\n")
        self.apps = self.root / "Applications"
        self.apps.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for tool in ("xcrun", "xcodebuild", "xcode-select", "uname"):
            path = self.bin / tool
            path.write_text(f"#!{sys.executable}\n" + MOCK_TOOL)
            path.chmod(0o755)
        self.config_path = self.root / "xcodes.json"
        self.log = self.root / "commands.jsonl"
        self.github_env = self.root / "github env"
        self.config = {"xcodes": {}}
        self.env = dict(os.environ)
        for key in ("DEVELOPER_DIR", "GITHUB_ENV", "BASH_ENV"):
            self.env.pop(key, None)
        self.env.update({
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "HOME": str(self.home),
            "CHROMIX_XCODE_APPLICATIONS_DIR": str(self.apps),
            "MOCK_XCODE_CONFIG": str(self.config_path),
            "MOCK_XCODE_LOG": str(self.log),
        })

    def add_xcode(self, name="Xcode_26.0.app", sdk="26.0", parent=None, **extra):
        developer = (parent or self.apps) / name / "Contents/Developer"
        sdk_path = developer / "Platforms/MacOSX.platform/Developer/SDKs" / f"MacOSX{sdk}.sdk"
        sdk_path.mkdir(parents=True)
        self.config["xcodes"][str(developer)] = {
            "sdk": sdk, "path": str(sdk_path), "xcode": "26.0", **extra,
        }
        return developer

    def run_helper(self, github=True, script=None):
        self.config_path.write_text(json.dumps(self.config))
        env = dict(self.env)
        if github:
            env["GITHUB_ENV"] = str(self.github_env)
        command = [str(self.BASH), "--norc"]
        if script is None:
            command += [str(self.selector)]
        else:
            command += ["-e", "-u", "-o", "pipefail", "-c", script,
                        "test", str(self.selector)]
        return subprocess.run(command, capture_output=True, text=True, env=env,
                              timeout=20)

    def assert_selected(self, result, developer):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.github_env.read_text(), f"DEVELOPER_DIR={developer}\n")
        self.assertIn(f"Selected DEVELOPER_DIR={developer}", result.stderr)
        self.assertIn("Build version MOCK", result.stderr)
        self.assertIn(self.config["xcodes"][str(developer)]["path"], result.stderr)

    def test_scripts_are_executable_and_parse(self):
        for script in (SELECT_XCODE, BUILD):
            self.assertTrue(os.access(script, os.X_OK))
            result = subprocess.run([str(self.BASH), "-n", str(script)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_old_default_selects_installed_sdk26_and_exports_to_next_step(self):
        old = self.add_xcode("Xcode.app", sdk="15.5", xcode="16.4")
        self.config["current"] = str(old)
        selected = self.add_xcode()
        result = self.run_helper()
        self.assert_selected(result, selected)
        self.assertIn("macOS SDK 15.5", result.stderr)
        self.assertIn("macOS SDK >= 26 required", result.stderr)
        self.env["DEVELOPER_DIR"] = self.github_env.read_text().strip().split("=", 1)[1]
        next_step = self.run_helper(github=False, script="xcrun --sdk macosx --show-sdk-version")
        self.assertEqual(next_step.stdout.strip(), "26.0")
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertTrue(all(args == ["--print-path"] for name, args, _ in calls
                            if name == "xcode-select"))

    def test_supported_explicit_selection_is_preserved_over_other_xcodes(self):
        selected = self.add_xcode("Custom Xcode.app", parent=self.root, sdk="26.1")
        self.config["current"] = str(self.add_xcode(sdk="26.0"))
        self.add_xcode("Xcode_27.app", sdk="27.0")
        self.env["DEVELOPER_DIR"] = str(selected)
        self.assert_selected(self.run_helper(), selected)

    def test_configured_app_path_and_trailing_slash_are_supported(self):
        selected = self.add_xcode("Custom Xcode.app", parent=self.root)
        self.env["DEVELOPER_DIR"] = str(selected.parent.parent) + "/"
        self.assert_selected(self.run_helper(), selected)

    def test_supported_active_xcode_outside_applications_is_preserved(self):
        selected = self.add_xcode("Custom Xcode.app", parent=self.root)
        self.config["current"] = str(selected)
        self.assert_selected(self.run_helper(), selected)

    def test_old_explicit_selection_falls_back(self):
        old = self.add_xcode("Xcode_16.4.app", sdk="15.5")
        self.env["DEVELOPER_DIR"] = str(old)
        selected = self.add_xcode()
        self.assert_selected(self.run_helper(), selected)

    def test_xcodes_subdirectory_and_symlink_are_discovered(self):
        actual = self.add_xcode("Custom.app", parent=self.root)
        folder = self.apps / "Xcodes"
        folder.mkdir()
        link = folder / "Xcode_26.app"
        link.symlink_to(actual.parent.parent, target_is_directory=True)
        selected = link / "Contents/Developer"
        self.config["xcodes"][str(selected)] = self.config["xcodes"][str(actual)]
        self.assert_selected(self.run_helper(), selected)

    def test_home_applications_are_discovered(self):
        selected = self.add_xcode(parent=self.home / "Applications")
        self.assert_selected(self.run_helper(), selected)

    def test_actual_sdk_version_not_app_name_controls_selection(self):
        self.add_xcode("Xcode_26.0.app", sdk="15.5")
        selected = self.add_xcode("Xcode_26.1.app", sdk="26")
        self.assert_selected(self.run_helper(), selected)

    def test_bad_candidates_do_not_hide_a_later_usable_xcode(self):
        self.add_xcode("Xcode_A.app", fail_sdk=True)
        self.add_xcode("Xcode_B.app", sdk="26.invalid")
        self.add_xcode("Xcode_C.app", fail_path=True)
        self.add_xcode("Xcode_D.app", path=str(self.root / "missing.sdk"))
        self.add_xcode("Xcode_E.app", fail_xcodebuild=True)
        selected = self.add_xcode("Xcode_Z.app", sdk="27.0")
        result = self.run_helper()
        self.assert_selected(result, selected)
        for diagnostic in ("xcrun failed", "26.invalid", "SDK path lookup failed",
                           "SDK path does not exist", "xcodebuild failed"):
            self.assertIn(diagnostic, result.stderr)

    def test_command_line_tools_are_not_full_xcode(self):
        clt = self.root / "CommandLineTools"
        clt.mkdir()
        self.config["current"] = str(clt)
        selected = self.add_xcode()
        result = self.run_helper()
        self.assert_selected(result, selected)
        self.assertIn("full Xcode is required", result.stderr)

    def test_only_old_sdks_fail_without_changing_github_environment(self):
        self.add_xcode("Xcode_9.app", sdk="9.9")
        self.add_xcode("Xcode_25.app", sdk="25.9")
        self.github_env.write_text("EXISTING=value\n")
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No usable installed Xcode provides macOS SDK >= 26", result.stderr)
        self.assertIn("macos-15-intel", result.stderr)
        self.assertEqual(self.github_env.read_text(), "EXISTING=value\n")

    def test_missing_xcodes_fail_early(self):
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Install Xcode 26 or newer", result.stderr)
        self.assertFalse(self.github_env.exists())

    def test_github_environment_write_failure_is_not_success(self):
        self.add_xcode()
        self.github_env.mkdir()
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Selected DEVELOPER_DIR=", result.stderr)

    def test_sourced_helper_exports_developer_dir_in_current_shell(self):
        selected = self.add_xcode()
        result = self.run_helper(github=False, script=(
            'source "$1"; select_macos_xcode; '
            'xcrun --sdk macosx --show-sdk-path'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), self.config["xcodes"][str(selected)]["path"])
        self.assertFalse(self.github_env.exists())

    def run_build(self):
        repo = self.root / "repo"
        (repo / "build/macos").mkdir(parents=True, exist_ok=True)
        shutil.copy2(BUILD, repo / "build/macos/build.sh")
        (repo / "build/posix").mkdir()
        shutil.copy2(REPO / "build/posix/upstream-cache.sh", repo / "build/posix/upstream-cache.sh")
        shutil.copy2(SELECT_XCODE, repo / "build/macos/select-xcode.sh")
        prepare = repo / "build/prepare-ungoogled.sh"
        prepare.write_text('#!/bin/sh\nprintenv DEVELOPER_DIR > "$1/selected"\nexit 77\n')
        prepare.chmod(0o755)
        self.config_path.write_text(json.dumps(self.config))
        work = self.root / "work"
        result = subprocess.run(
            [str(self.BASH), str(repo / "build/macos/build.sh"), str(work), "arm64"],
            capture_output=True, text=True, env=self.env, timeout=20)
        return result, work

    def test_build_exports_selection_before_prepare(self):
        selected = self.add_xcode()
        result, work = self.run_build()
        self.assertEqual(result.returncode, 77, result.stdout + result.stderr)
        self.assertEqual((work / "selected").read_text().strip(), str(selected))

    def test_build_fails_before_prepare_without_supported_sdk(self):
        self.add_xcode("Xcode_16.4.app", sdk="15.5")
        result, work = self.run_build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No usable installed Xcode", result.stderr)
        self.assertFalse(work.exists())


@unittest.skipUnless(BASH32.is_file(), "locally built Bash 3.2 required")
class MacOSToolchainBash32Test(MacOSToolchainTest):
    BASH = BASH32


if __name__ == "__main__":
    unittest.main()
