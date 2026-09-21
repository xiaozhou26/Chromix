"""Guard restored Ninja state using tiny headers and mocked version probes."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools import restore_ninja as guard
from tools.platform_pins import load_pins

REPO = Path(__file__).resolve().parents[2]
BASH32 = Path("/root/.local/bash-3.2-for-ci/bash")
BASH32_AVAILABLE = os.access(BASH32, os.X_OK)
PWSH = Path("/opt/pwsh/pwsh")
TARGETS = (("linux", "x64"), ("linux", "arm64"), ("macos", "x64"),
           ("macos", "arm64"), ("windows", "x64"))


def binary_header(system, arch):
    data = bytearray(64)
    if system == "linux":
        data[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", data, 16, 2)
        struct.pack_into("<H", data, 18, 62 if arch == "x64" else 183)
    elif system == "macos":
        data[:4] = b"\xcf\xfa\xed\xfe"
        struct.pack_into("<I", data, 4, 0x1000007 if arch == "x64" else 0x100000C)
        struct.pack_into("<I", data, 12, 2)
    else:
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 60, 64)
        data.extend(b"PE\0\0" + struct.pack("<H", 0x8664 if arch == "x64" else 0xAA64))
    return bytes(data)


class RestoreNinjaTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="restored ninja spaces ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / "work"
        self.src = self.work / "src"
        self.out = self.src / "out/Default"
        self.out.mkdir(parents=True)
        (self.src / ".chromix-upstream-restored.json").write_text("{}")
        self.log = self.out / ".ninja_log"
        self.log.write_bytes(b"# ninja log v6\n0\t1\t100\tobj/a.o\tabc\n")
        self.versions = {}
        self.probed = []
        self.host_dir = self.root / "host bin"
        self.host_dir.mkdir()
        self.patch = mock.patch.dict(os.environ, {"PATH": str(self.host_dir)})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def put_binary(self, system="linux", arch="x64", version="1.12.1", *, bundled=False, directory=None):
        name = "ninja.exe" if system == "windows" else "ninja"
        path = (self.src / "third_party/ninja" if bundled else directory or self.host_dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(binary_header(system, arch))
        path.chmod(0o755)
        self.versions[path.resolve()] = version
        return path

    def probe(self, path, work):
        self.assertEqual(work, self.work)
        self.probed.append(path)
        value = self.versions[path]
        if isinstance(value, Exception):
            raise value
        return value

    def select(self, system="linux", arch="x64", *, host=None):
        before = self.log.read_bytes() if self.log.exists() else None
        with mock.patch.object(guard, "host_identity", return_value=host or (system, arch)), \
                mock.patch.object(guard, "probe_version", side_effect=self.probe):
            try:
                return guard.select_ninja(self.work, system, arch)
            finally:
                self.assertEqual(self.log.read_bytes() if self.log.exists() else None, before)

    def report(self):
        return json.loads((self.work / guard.REPORT).read_text())

    def test_all_five_targets_and_three_formats(self):
        for system, arch in TARGETS:
            for log_format, version in ((5, "1.11.1"), (6, "1.12.1"), (7, "1.13.2")):
                with self.subTest(system=system, arch=arch, log=log_format):
                    self.log.write_bytes(f"# ninja log v{log_format}\r\n".encode())
                    path = self.put_binary(system, arch, version, bundled=system == "windows")
                    self.assertEqual(self.select(system, arch), path.resolve())
                    self.assertEqual(self.report()["log"]["format"], log_format)
                    self.assertEqual(self.report()["selected"]["version"], version)

    def test_linux_arm64_target_uses_x64_host_ninja_for_all_log_formats(self):
        for log_format, version in ((5, "1.11.1"), (6, "1.12.1.chromium.4"), (7, "1.13.2")):
            with self.subTest(log=log_format):
                self.log.write_bytes(f"# ninja log v{log_format}\n".encode())
                host = self.put_binary(version=version)
                bundle = self.put_binary(arch="arm64", version=version, bundled=True)
                self.probed.clear()
                self.assertEqual(self.select("linux", "arm64", host=("linux", "x64")), host)
                self.assertEqual(self.probed, [host])
                report = self.report()
                self.assertEqual((report["platform"], report["arch"]), ("linux", "arm64"))
                self.assertEqual(report["host"], ["linux", "x64"])
                self.assertEqual(report["selected"]["architectures"], ["x64"])
                self.assertEqual(report["log"]["format"], log_format)
                host.unlink()
                self.probed.clear()
                with self.assertRaisesRegex(ValueError, "no compatible native Ninja"):
                    self.select("linux", "arm64", host=("linux", "x64"))
                self.assertEqual(self.probed, [])
                self.assertIn("header", self.report()["candidates"][-1]["reason"])
                self.put_binary(version=version, bundled=True)
                self.assertEqual(self.select("linux", "arm64", host=("linux", "x64")), bundle)
                self.assertEqual(self.probed, [bundle])

    def test_linux_cross_does_not_relax_executable_header_or_version_checks(self):
        host = self.put_binary()
        headers = [binary_header("linux", "arm64"), binary_header("macos", "x64"),
                   binary_header("linux", "x64")[:63], b"#!/bin/sh\nexit 0\n"]
        relocatable = bytearray(binary_header("linux", "x64"))
        struct.pack_into("<H", relocatable, 16, 1)
        headers.append(relocatable)
        for header in headers:
            with self.subTest(header=header[:20]):
                host.write_bytes(header)
                with self.assertRaisesRegex(ValueError, "no compatible native Ninja"):
                    self.select("linux", "arm64", host=("linux", "x64"))
                self.assertEqual(self.probed, [])
        for version in ("1.11.1", "1.13.2", "1.14.0", "1.12.1-git", "1.12", "1.12.1\nwarning"):
            with self.subTest(version=version):
                self.put_binary(version=version)
                with self.assertRaisesRegex(ValueError, "requires 1.12.x"):
                    self.select("linux", "arm64", host=("linux", "x64"))
                self.assertEqual(self.report()["status"], "failed")

    def test_linux_cross_does_not_relax_log_header_checks(self):
        self.put_binary()
        for header in (b"# ninja log v8\n", b"# ninja log v06\n", b"# ninja log v6",
                       b"# ninja log v6 \n", b"", b"X" * 200):
            with self.subTest(header=header[:30]):
                self.log.write_bytes(header)
                with self.assertRaisesRegex(ValueError, "unknown restored Ninja log header"):
                    self.select("linux", "arm64", host=("linux", "x64"))
                self.assertEqual(self.probed, [])

    def test_compatibility_map_does_not_guess_future_versions(self):
        for version, expected in (("1.10.1", None), ("1.11.1", 5), ("1.12.1", 6),
                                  ("1.12.1.chromium.4", 6), ("1.13.2", 7),
                                  ("1.14.0", None), ("2.0.0", None), ("1.9.0", None),
                                  ("1.13.2-git", None), ("1.13", None)):
            self.assertEqual(guard.version_format(version), expected, version)

    def test_compatible_posix_host_wins_over_bundle(self):
        host = self.put_binary()
        self.put_binary(bundled=True)
        self.assertEqual(self.select(), host)
        self.assertEqual(self.probed, [host])

    def test_windows_arm64_uses_only_x64_host_ninja_for_each_log_format(self):
        for log_format, version in ((5, "1.11.1"), (6, "1.12.1.chromium.4"), (7, "1.13.2")):
            with self.subTest(log=log_format):
                self.log.write_bytes(f"# ninja log v{log_format}\r\n".encode())
                bundle = self.put_binary("windows", "arm64", version, bundled=True)
                host = self.put_binary("windows", "x64", version)
                self.probed.clear()
                self.assertEqual(self.select("windows", "arm64", host=("windows", "x64")), host)
                self.assertEqual(self.probed, [host])
                report = self.report()
                self.assertEqual(report["arch"], "arm64")
                self.assertEqual(report["host"], ["windows", "x64"])
                self.assertEqual(report["candidates"][0]["architectures"], ["arm64"])
                self.assertEqual(report["selected"]["architectures"], ["x64"])
                host.unlink()
                self.probed.clear()
                with self.assertRaisesRegex(ValueError, "no compatible native Ninja"):
                    self.select("windows", "arm64", host=("windows", "x64"))
                self.assertEqual(self.probed, [])
                self.put_binary("windows", "x64", version, bundled=True)
                self.assertEqual(self.select("windows", "arm64", host=("windows", "x64")), bundle)
                self.versions[bundle] = "1.14.0"
                with self.assertRaisesRegex(ValueError, "no compatible native Ninja"):
                    self.select("windows", "arm64", host=("windows", "x64"))

    def test_windows_prefers_bundle_then_falls_back_to_host(self):
        host = self.put_binary("windows")
        bundle = self.put_binary("windows", bundled=True)
        self.assertEqual(self.select("windows"), bundle)
        self.versions[bundle] = "1.13.2"
        self.assertEqual(self.select("windows"), host)

    def test_incompatible_posix_host_uses_native_bundle(self):
        for system, arch in TARGETS[:-1]:
            with self.subTest(system=system, arch=arch):
                self.put_binary(system, arch, "1.13.2")
                bundle = self.put_binary(system, arch, bundled=True)
                self.assertEqual(self.select(system, arch), bundle)
                self.assertEqual(self.report()["candidates"][0]["status"], "rejected")

    def test_tries_later_path_candidate_before_bundle(self):
        self.put_binary(version="1.13.2")
        later = self.put_binary(directory=self.root / "later")
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join((str(self.host_dir), str(later.parent)))}):
            self.assertEqual(self.select(), later)

    def test_wrong_arch_bundle_is_never_executed(self):
        for system, arch in TARGETS:
            with self.subTest(system=system, arch=arch):
                self.probed.clear()
                self.put_binary(system, arch, "1.14.0")
                wrong = "arm64" if arch == "x64" else "x64"
                bundle = self.put_binary(system, wrong, bundled=True)
                with self.assertRaisesRegex(ValueError, "no compatible"):
                    self.select(system, arch)
                self.assertNotIn(bundle, self.probed)
                self.assertTrue(any("header" in item.get("reason", "") for item in self.report()["candidates"]))

    def test_wrong_platform_and_script_candidates_are_not_probed(self):
        for payload in (binary_header("macos", "x64"), b"#!/bin/sh\nexit 0\n", b"MZ"):
            host = self.put_binary()
            host.write_bytes(payload)
            with self.assertRaisesRegex(ValueError, "no compatible"):
                self.select()
            self.assertEqual(self.probed, [])

    def test_unknown_or_missing_log_fails_before_any_probe(self):
        self.put_binary()
        for header in (b"# ninja log v8\n", b"# ninja log v07\n", b"# ninja log v7", b"", b"X" * 200):
            with self.subTest(header=header[:30]):
                self.log.write_bytes(header)
                with self.assertRaisesRegex(ValueError, "unknown restored"):
                    self.select()
                self.assertEqual(self.probed, [])
                self.assertLessEqual(len(self.report()["log"]["header_hex"]), 256)
        self.log.unlink()
        with self.assertRaises(FileNotFoundError):
            self.select()
        self.assertEqual(self.probed, [])

    def test_no_compatible_candidate_preserves_log(self):
        self.put_binary(version="1.13.2")
        self.put_binary(version="1.14.0", bundled=True)
        with self.assertRaisesRegex(ValueError, "requires 1.12.x"):
            self.select()
        self.assertEqual(self.report()["status"], "failed")

    def test_timeout_or_probe_failure_uses_bundle(self):
        host = self.put_binary()
        bundle = self.put_binary(bundled=True)
        for error in (subprocess.TimeoutExpired([str(host)], 5), ValueError("bad version"), OSError("cannot run")):
            self.versions[host] = error
            self.assertEqual(self.select(), bundle)

    def test_only_native_posix_linux_cross_and_x64_hosted_windows_pairs_are_allowed(self):
        for target in (*TARGETS, ("windows", "arm64")):
            for host in (*TARGETS, ("windows", "arm64"), ("linux", "unknown")):
                allowed = ((target[0], "x64") if target[0] == "windows" else target)
                if host == allowed or (target, host) == (("linux", "arm64"), ("linux", "x64")):
                    continue
                with self.subTest(target=target, host=host):
                    with self.assertRaisesRegex(ValueError, "native .* runner is required"):
                        self.select(*target, host=host)
                    self.assertEqual(self.probed, [])
                    self.assertEqual(self.report()["host"], list(host))
                    self.assertEqual(self.report()["candidates"], [])
        with self.assertRaisesRegex(ValueError, "unsupported restored build target"):
            self.select("windows", "x86")

    def test_bundle_cannot_link_outside_source(self):
        external = self.put_binary(version="1.13.2")
        bundle = self.src / "third_party/ninja/ninja"
        bundle.parent.mkdir(parents=True)
        bundle.symlink_to(external)
        with mock.patch.dict(os.environ, {"PATH": ""}):
            with self.assertRaisesRegex(ValueError, "no compatible"):
                self.select()
        self.assertEqual(self.probed, [])

    def test_macho_universal_headers(self):
        path = self.host_dir / "universal"
        for magic, width in ((b"\xca\xfe\xba\xbe", 20), (b"\xca\xfe\xba\xbf", 32)):
            data = bytearray(8 + 2 * width)
            data[:4] = magic
            struct.pack_into(">I", data, 4, 2)
            struct.pack_into(">I", data, 8, 0x1000007)
            struct.pack_into(">I", data, 8 + width, 0x100000C)
            path.write_bytes(data.ljust(64, b"\0"))
            self.assertEqual(guard.binary_architectures(path, "macos"), {"x64", "arm64"})
            struct.pack_into(">I", data, 4, 1000000)
            path.write_bytes(data.ljust(64, b"\0"))
            self.assertEqual(guard.binary_architectures(path, "macos"), set())

    def test_probe_output_is_bounded_and_failures_rejected(self):
        def run_output(payload, returncode=0):
            def run(command, **kwargs):
                self.assertEqual(command[1:], ["--version"])
                self.assertEqual(kwargs["timeout"], 5)
                kwargs["stdout"].write(payload)
                return subprocess.CompletedProcess(command, returncode)
            return run
        for payload, code in ((b"x" * 1025, 0), (b"1.12.1\nwarning\n", 0), (b"1.12.1", 2)):
            with mock.patch.object(guard.subprocess, "run", side_effect=run_output(payload, code)):
                with self.assertRaises(ValueError):
                    guard.probe_version(self.host_dir / "ninja", self.work)
        with mock.patch.object(guard.subprocess, "run", side_effect=run_output(b"1.12.1\n")):
            self.assertEqual(guard.probe_version(self.host_dir / "ninja", self.work), "1.12.1")

    def test_real_native_ninja_version_probe_never_opens_output_tree(self):
        ninja = Path("/usr/bin/ninja")
        if guard.host_identity() != ("linux", "x64") or not ninja.is_file():
            self.skipTest("native Linux Ninja required")
        version = guard.probe_version(ninja, self.work)
        log_format = guard.version_format(version)
        if log_format is None:
            self.skipTest("installed Ninja family is not in the verified map")
        self.log.write_bytes(f"# ninja log v{log_format}\n".encode())
        before = self.log.read_bytes()
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
            result = subprocess.run([sys.executable, str(REPO / "tools/restore_ninja.py"),
                                     "--workdir", str(self.work), "--platform", "linux", "--arch", "x64"],
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(ninja.resolve()))
        self.assertEqual(self.log.read_bytes(), before)
        self.assertEqual(list(self.out.iterdir()), [self.log])

    def test_cli_stdout_is_only_absolute_selected_path(self):
        host = self.put_binary()
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(guard, "host_identity", return_value=("linux", "x64")), \
                mock.patch.object(guard, "probe_version", side_effect=self.probe), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(guard.main(["--workdir", str(self.work), "--platform", "linux", "--arch", "x64"]), 0)
        self.assertEqual(stdout.getvalue(), str(host) + "\n")
        self.assertIn("1.12.1 for log v6", stderr.getvalue())


class RestoreNinjaShellTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ninja shell spaces ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.work = self.root / "work"
        self.src = self.work / "src"
        self.out = self.src / "out/Default"
        self.out.mkdir(parents=True)
        (self.repo / "tools").mkdir(parents=True)
        self.calls = self.root / "calls"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.chosen = self.bin / "selected ninja"
        self.chosen.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$CALLS"\n')
        self.chosen.chmod(0o755)
        (self.repo / "tools/restore_ninja.py").write_text(
            'import os, sys\n'
            'print("guard diagnostic", file=sys.stderr)\n'
            'if os.environ.get("FAIL_GUARD"): sys.exit(9)\n'
            'print(os.environ["CHOSEN"])\n')
        self.env = {**os.environ, "REPO": str(self.repo), "WORK": str(self.work),
                    "SRC": str(self.src), "OUT": str(self.out), "ARCH": "x64",
                    "CHOSEN": str(self.chosen), "CALLS": str(self.calls)}
        self.env.pop("CHROMIX_UPSTREAM_CACHE_DIR", None)

    @unittest.skipUnless(BASH32_AVAILABLE, "Executable Bash 3.2 required")
    def test_bash32_selected_path_for_plan_build_and_bootstrap(self):
        (self.src / ".chromix-upstream-restored.json").touch()
        script = 'source "$1"; chromix_select_restored_ninja linux; test "$NINJA" = "$CHOSEN"; '
        script += 'chromix_report_upstream_plan chrome; "$CHROMIX_NINJA" -C "$OUT" chrome'
        result = subprocess.run([str(BASH32), "-euo", "pipefail", "-c", script, "fixture",
                                 str(REPO / "build/posix/upstream-cache.sh")],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(line.startswith(str(self.chosen) + " ") for line in calls))
        self.assertIn("-n chrome", calls[0])
        self.assertIn("guard diagnostic", result.stderr)

    @unittest.skipUnless(BASH32_AVAILABLE, "Executable Bash 3.2 required")
    def test_bash32_guard_failure_prevents_ninja(self):
        (self.src / ".chromix-upstream-restored.json").touch()
        result = subprocess.run([str(BASH32), "-euo", "pipefail", "-c",
                                 'source "$1"; chromix_select_restored_ninja linux; chromix_report_upstream_plan chrome',
                                 "fixture", str(REPO / "build/posix/upstream-cache.sh")],
                                env={**self.env, "FAIL_GUARD": "1"}, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls.exists())

    @unittest.skipUnless(BASH32_AVAILABLE, "Executable Bash 3.2 required")
    def test_bash32_cold_build_does_not_call_guard_or_change_ninja_env(self):
        result = subprocess.run([str(BASH32), "-euo", "pipefail", "-c",
                                 'source "$1"; chromix_select_restored_ninja linux; '
                                 'test "$CHROMIX_NINJA" = ninja; test "$NINJA" = existing',
                                 "fixture", str(REPO / "build/posix/upstream-cache.sh")],
                                env={**self.env, "FAIL_GUARD": "1", "NINJA": "existing"},
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("guard diagnostic", result.stderr)

    @unittest.skipUnless(BASH32_AVAILABLE, "Executable Bash 3.2 required")
    def test_all_four_posix_builders_keep_selected_plan_and_build_path(self):
        def shell(path, body):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/bin/sh\n" + body)
            path.chmod(0o755)

        for relative in ("build/build.sh", "build/macos/build.sh", "build/posix/upstream-cache.sh",
                         "tools/bootstrap_gn.py", "tools/merge_gn_args.py", "tools/macos_runtime.py"):
            destination = self.repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / relative, destination)
        (self.repo / "tools/restored_reuse_evidence.py").write_text("import sys\nassert '--phase' in sys.argv\n")
        # Patch contents are covered by test_verify_patch_stack.py; this fixture checks call wiring.
        (self.repo / "tools/verify_patch_stack.py").write_text(
            'import os, sys\nfrom pathlib import Path\n'
            'args = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n'
            'assert args["--src"] == os.environ["SRC"]\n'
            'assert args["--repo"] == os.environ["REPO"]\n'
            'assert args["--platform"] in ("linux", "macos")\n'
            'output = Path(args["--output"])\n'
            'assert output.parent == Path(os.environ["WORK"]) / "fingerprint-diagnostics"\n'
            'output.write_text("{}\\n")\n')
        shell(self.repo / "build/prepare-ungoogled.sh", "exit 0\n")
        shell(self.repo / "build/posix/prepare-restored-tools.sh", 'touch "$1/src/.chromix-toolchain-ready"\n')
        shell(self.repo / "build/macos/select-xcode.sh", "select_macos_xcode() { :; }\n")
        for name in (".chromix-upstream-restored.json", ".chromix-domain-substituted"):
            (self.src / name).touch()
        for name in ("node", "go", "gperf", "clang-format"):
            shell(self.bin / name, "exit 0\n")
        shell(self.bin / "ninja", 'printf "WRONG NINJA\\n" >> "$CALLS"\nexit 99\n')
        shell(self.bin / "sysctl", 'printf "2\\n"\n')
        shell(self.out / "gn", '[ "$1" != --version ] || exit 0\nprintf "gn\\n" >> "$CALLS"\n')
        shell(self.out / "chrome", "exit 0\n")
        for system, arch in TARGETS[:-1]:
            with self.subTest(system=system, arch=arch):
                machine = "x86_64" if arch == "x64" else "aarch64" if system == "linux" else "arm64"
                os_name = "Linux" if system == "linux" else "Darwin"
                shell(self.bin / "uname", f'case "$1" in -m) printf "{machine}\\n";; -s) printf "{os_name}\\n";; esac\n')
                overlay = "args.gn" if system == "linux" else "args.macos.gn"
                (self.repo / "build" / overlay).write_text("symbol_level = 0\n")
                platform_repo = "ungoogled-chromium-" + ("portablelinux" if system == "linux" else "macos")
                for directory, name in (("ungoogled-chromium", "flags.gn"), (platform_repo, f"flags.{system}.gn")):
                    path = self.work / "tooling" / directory / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("symbol_level = 1\n")
                (self.out / "args.gn").write_text("upstream_extra = true\n")
                self.calls.unlink(missing_ok=True)
                builder = "build/build.sh" if system == "linux" else "build/macos/build.sh"
                result = subprocess.run([str(BASH32), str(self.repo / builder), str(self.work), arch],
                                        env={**self.env, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                                             "CHROMIX_SKIP_DEPS": "1", "CHROMIX_JOBS": "2"},
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                calls = self.calls.read_text().splitlines()
                self.assertEqual(calls[0], "gn")
                self.assertEqual(len(calls), 3)
                self.assertTrue(all(call.startswith(str(self.chosen)) for call in calls[1:]))
                self.assertIn("-n chrome", calls[1])
                self.assertIn("upstream_extra = true", (self.out / "args.gn").read_text())

    @unittest.skipUnless(PWSH.exists(), "PowerShell required")
    def test_powershell_owned_scripts_parse(self):
        script = 'param($Files); foreach ($file in ($Files -split ";")) { '
        script += '$tokens=$null; $errors=$null; '
        script += '[System.Management.Automation.Language.Parser]::ParseFile($file,[ref]$tokens,[ref]$errors) | Out-Null; '
        script += 'if ($errors.Count) { throw ($errors | Out-String) } }'
        path = self.root / "parse.ps1"
        path.write_text(script)
        result = subprocess.run([str(PWSH), "-NoProfile", "-File", str(path),
                                 ";".join(str(REPO / f"build/windows/{name}.ps1") for name in ("build", "ci-stage"))],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(PWSH.exists(), "PowerShell required")
    def test_powershell_selection_blocks_stop_on_failure_and_use_path_with_spaces(self):
        (self.src / ".chromix-upstream-restored.json").touch()
        # Native Node execution is covered by test_windows_node.py.
        (self.root / "configure-node.ps1").write_text('param($NodePath)\n')
        for name in ("build", "ci-stage"):
            source = (REPO / f"build/windows/{name}.ps1").read_text()
            start = source.index('$Ninja = Join-Path $Src "third_party\\ninja\\ninja.exe"')
            end = source.index('\n$mergedArgs =', start) if name == "build" else source.index('\nPush-Location $Src', start)
            block = source[start:end]
            for fail in (False, True):
                with self.subTest(script=name, fail=fail):
                    self.calls.unlink(missing_ok=True)
                    script = '$ErrorActionPreference="Stop"\n'
                    script += "Set-Alias python '" + sys.executable.replace("'", "''") + "'\n"
                    script += '$Repo=$env:REPO; $WorkDir=$env:WORK; $Src=$env:SRC; $Out=$env:OUT; $RestoredUpstream=$true\n'
                    script += block + '\n& $Ninja -C $Out -n chrome\n& $Ninja -C $Out chrome\n'
                    script += 'if ($env:NINJA -ne $env:CHOSEN) { throw "bootstrap Ninja differs" }\n'
                    path = self.root / "selection.ps1"
                    path.write_text(script)
                    result = subprocess.run([str(PWSH), "-NoProfile", "-File", str(path)],
                                            env={**self.env, "FAIL_GUARD": "1" if fail else ""},
                                            capture_output=True, text=True, timeout=20)
                    if fail:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertFalse(self.calls.exists())
                    else:
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_build_scripts_use_selected_executable_after_guard(self):
        for name, system in (("build/build.sh", "linux"), ("build/macos/build.sh", "macos")):
            source = (REPO / name).read_text()
            self.assertLess(source.index(f"chromix_select_restored_ninja {system}"), source.index('"$OUT/gn" gen'))
            self.assertIn(f"chromix_build_restored_target {system}", source)
            helper = (REPO / "build/posix/upstream-cache.sh").read_text()
            self.assertIn('"$CHROMIX_NINJA" -C "$OUT" -j "$jobs" "$@"', helper)
        stage = (REPO / "build/windows/ci-stage.ps1").read_text()
        self.assertEqual(stage.count("Invoke-Tracked -File $Ninja"), 2)
        self.assertIn("& $Ninja -C $OutDir -n chrome", stage)
        for name in ("build", "ci-stage"):
            source = (REPO / f"build/windows/{name}.ps1").read_text()
            self.assertLess(source.index("restore_ninja.py"), source.index("tools\\gn\\bootstrap\\bootstrap.py"))
            self.assertNotIn("--ninja-executable", source)
        direct = (REPO / "build/windows/build.ps1").read_text()
        self.assertIn('$Out = Join-Path $Src "out\\Default"', direct)
        self.assertLess(direct.index('$mergeArgs += $mergedArgs'), direct.index('$mergeArgs += @('))


@unittest.skipUnless(PWSH.exists(), "PowerShell required")
class DirectWindowsRestoredBuildTest(unittest.TestCase):
    def run_builder(self, *, restored=True, bindgen_present=False, fail="", ninja_rc=0, resume=False,
                    product_version=None, metadata_present=True, chrome_present=True):
        if product_version is None:
            product_version = load_pins(REPO, "windows")["ChromiumVersion"]
        temporary = tempfile.TemporaryDirectory(prefix="direct windows restored ")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        repo, work = root / "repo", root / "work"
        src = work / "src"
        out = src / "out" / ("Default" if restored else "Chromix")
        out.mkdir(parents=True)
        calls = root / "calls"
        chosen = root / "selected ninja.exe"
        env = {**os.environ, "TEST_PYTHON": sys.executable, "TEST_REPO": str(repo),
               "TEST_WORK": str(work), "TEST_OUT": str(out), "TEST_CALLS": str(calls),
               "TEST_NINJA": str(chosen), "TEST_FAIL": fail, "TEST_RESTORED": str(int(restored)),
               "TEST_NINJA_RC": str(ninja_rc), "TEST_RESUME": str(int(resume)),
               "TEST_PRODUCT_VERSION": product_version, "TEST_METADATA_PRESENT": str(int(metadata_present)),
               "TEST_CHROME_PRESENT": str(int(chrome_present))}
        for name in ("NINJA", "PYTHONPATH", "PYTHONHOME"):
            env.pop(name, None)

        def put(path, content, executable=False):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            if executable:
                path.chmod(0o755)

        for relative in ("build/windows/build.ps1", "build/windows/assert-target-arch.ps1",
                         "build/windows/read-platform-pins.ps1", "build/ungoogled-revisions.psd1",
                         "CHROMIUM_VERSION", "CHROMIUM_WINDOWS_VERSION", "tools/platform_pins.py",
                         "tools/merge_gn_args.py", "tools/upstream_script_identity.py"):
            destination = repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / relative, destination)
        # Native Node execution is covered by test_windows_node.py.
        put(repo / "build/windows/configure-node.ps1", 'param($NodePath)\n')
        # Native SDK provisioning is covered by test_windows_sdk.py.
        put(repo / "build/windows/ensure-windows-sdk.ps1", 'param($Arch, $ChromiumVersion)\n')
        put(repo / "build/args.windows.gn", "symbol_level = 0\n")
        for directory, name in (("ungoogled-chromium", "flags.gn"), ("ungoogled-chromium-windows", "flags.windows.gn")):
            put(work / "tooling" / directory / name, "symbol_level = 1\n")
        put(out / "args.gn", "upstream_extra = true\nsymbol_level = 2\n")
        put(out / ".ninja_log", "# ninja log v6\n")
        put(out / ".ninja_deps", "retained deps")
        put(out / "changed-compiler.obj", "upstream object")
        if restored:
            put(src / ".chromix-upstream-restored.json", json.dumps({
                "platform": "windows", "arch": "x64",
                "identity": {"platform": "windows", "arch": "x64"}}))
        baseline = work / 'upstream-reuse/baseline.json'
        if resume:
            put(src / '.chromix-source-ready', 'ready')
            put(baseline, '{"original": true}')
            os.utime(baseline, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))

        common = (
            "import os, sys\nfrom pathlib import Path\n"
            "work = Path(os.environ['TEST_WORK'])\nsrc = work / 'src'\n"
            "out = Path(os.environ['TEST_OUT'])\n"
            "def record(name):\n"
            "    with Path(os.environ['TEST_CALLS']).open('a') as stream: stream.write(name + '\\n')\n"
            "    if os.environ.get('TEST_FAIL') == name: raise SystemExit(23)\n"
        )
        executable = "#!" + sys.executable + "\n" + common
        good_gn = executable + "record('gn')\nassert sys.argv[1:] == ['gen', str(out), '--fail-on-unused-args']\n"
        chrome = executable + "record('chrome')\nraise SystemExit('browser must not run during metadata validation')\n"
        put(out / "gn.exe", executable + "record('wrong-gn')\nraise SystemExit(99)\n" if restored else good_gn, True)
        put(out / "chrome.exe", executable + "record('upstream-chrome')\nraise SystemExit(99)\n", True)
        put(src / "chrome/browser-fixture.cc", "chromium.9oo91esource.qjz9zk\n")

        from tools.upstream_script_identity import ENDPOINTS, RESTORED
        for relative, keys in RESTORED.items():
            put(src / relative, "\n".join("# " + ENDPOINTS[key][0] for key in keys) + "\n")
        bindgen = common + "# chromium.9oo91esource.qjz9zk\nrecord('bindgen')\n"
        bindgen += "if os.environ['TEST_RESTORED'] == '1':\n"
        bindgen += "    assert 'chromium.googlesource.com' in Path(__file__).read_text()\n"
        bindgen += "    assert os.environ['NINJA'] == os.environ['TEST_NINJA']\n"
        bindgen += "(src / 'third_party/rust-toolchain/bin').mkdir(parents=True, exist_ok=True)\n"
        bindgen += "(src / 'third_party/rust-toolchain/bin/bindgen.exe').write_text('native bindgen')\n"
        put(src / "tools/rust/build_bindgen.py", bindgen)
        if bindgen_present:
            put(src / "third_party/rust-toolchain/bin/bindgen.exe", "native bindgen")
        if fail == "normalize":
            (src / "tools/clang/scripts/update.py").unlink()

        put(repo / "build/windows/prepare-ungoogled.ps1", r'''param($Root, $Repo)
if ($env:TEST_RESTORED -eq '1') {
  python (Join-Path $Repo "tools/prepare_restored_build.py") --phase inspect --platform windows --arch x64 --workdir $Root
  if ($LASTEXITCODE -ne 0) { throw "inspection failed" }
}
Add-Content -LiteralPath $env:TEST_CALLS -Value 'prep-patch'
if ($env:TEST_FAIL -eq 'prep-patch') { throw "preparation failed" }
''')
        finish = common + "phase = sys.argv[sys.argv.index('--phase') + 1]\nrecord(phase)\n"
        finish += "assert sys.argv[1:] == ['--phase', phase, '--platform', 'windows', '--arch', 'x64', '--workdir', str(work)]\n"
        finish += "if phase == 'finish':\n"
        finish += "    assert (src / 'third_party/rust-toolchain/bin/bindgen.exe').is_file()\n"
        finish += "    assert 'upstream_extra = true' in (out / 'args.gn').read_text()\n"
        finish += "    assert 'wrong-gn' in (out / 'gn.exe').read_text()\n"
        finish += "    for name in ('gn.exe', 'chrome.exe', 'changed-compiler.obj'): (out / name).unlink()\n"
        put(repo / "tools/prepare_restored_build.py", finish)
        put(repo / "tools/restore_ninja.py", common + "record('select')\nprint(os.environ['TEST_NINJA'])\n")
        evidence = common + '''import argparse, json
parser = argparse.ArgumentParser()
parser.add_argument('--phase', choices=('before', 'after'), required=True)
parser.add_argument('--workdir', required=True)
parser.add_argument('--platform', choices=('windows',), required=True)
parser.add_argument('--arch', choices=('x64',), required=True)
parser.add_argument('--ninja', required=True)
parser.add_argument('--target', choices=('chrome',), required=True)
parser.add_argument('--exit-code', type=int)
args = parser.parse_args()
assert args.workdir == str(work) and args.ninja == os.environ['TEST_NINJA']
assert Path(os.environ['TEST_CALLS']).read_text().splitlines()[-1] == ('plan' if args.phase == 'before' else 'ninja')
record('evidence-' + args.phase)
directory = work / 'upstream-reuse'
directory.mkdir(exist_ok=True)
baseline = directory / 'baseline.json'
if args.phase == 'before':
    assert args.exit_code is None
    if not baseline.exists():
        baseline.write_text('{}')
else:
    assert baseline.is_file()
    assert args.exit_code == int(os.environ['TEST_NINJA_RC'])
    (directory / 'result.json').write_text(json.dumps({'exit_code': args.exit_code}))
print(json.dumps({'phase': args.phase}))
'''
        put(repo / "tools/restored_reuse_evidence.py", evidence)
        bootstrap = common + "record('bootstrap')\n"
        bootstrap += "assert sys.argv[1:] == ['-o', str(out / 'gn.exe'), '--skip-generate-buildfiles']\n"
        bootstrap += "if os.environ['TEST_RESTORED'] == '1':\n"
        bootstrap += "    assert os.environ['NINJA'] == os.environ['TEST_NINJA']\n"
        bootstrap += "    assert not (out / 'gn.exe').exists() and not (out / 'chrome.exe').exists()\n"
        bootstrap += "(out / 'gn.exe').write_text(" + repr(good_gn) + ")\n(out / 'gn.exe').chmod(0o755)\n"
        put(src / "tools/gn/bootstrap/bootstrap.py", bootstrap)
        ninja = executable + "if '-n' in sys.argv:\n"
        ninja += "    record('plan')\n    assert sys.argv[1:] == ['-C', str(out), '-n', 'chrome']\n    raise SystemExit(0)\n"
        ninja += "record('ninja')\n"
        ninja += "assert sys.argv[1:] == ['-C', str(out), '-j', '3', 'chrome']\n"
        ninja += "if os.environ['TEST_RESTORED'] == '1': assert (work / 'upstream-reuse/baseline.json').is_file()\n"
        ninja += "if int(os.environ['TEST_NINJA_RC']): raise SystemExit(int(os.environ['TEST_NINJA_RC']))\n"
        ninja += "if os.environ['TEST_CHROME_PRESENT'] == '0':\n    (out / 'chrome.exe').unlink(missing_ok=True)\n    raise SystemExit(0)\n"
        ninja += "(out / 'chrome.exe').write_text(" + repr(chrome) + ")\n(out / 'chrome.exe').chmod(0o755)\n"
        put(chosen, ninja, True)
        put(src / "third_party/ninja/ninja.exe", executable + "record('wrong-ninja')\nraise SystemExit(99)\n" if restored else ninja, True)
        runner = root / "run.ps1"
        put(runner, r'''$ErrorActionPreference = 'Stop'
function python {
  $pythonArgs = @($args)
  if ($pythonArgs[0] -ne '-c') { $pythonArgs[0] = $pythonArgs[0].Replace('\', '/') }
  & $env:TEST_PYTHON @pythonArgs
  $global:LASTEXITCODE = $LASTEXITCODE
}
function Get-Item {
  param($LiteralPath)
  if ($LiteralPath -ne (Join-Path $env:TEST_OUT 'chrome.exe')) { throw "unexpected version metadata path: $LiteralPath" }
  if (-not (Test-Path -LiteralPath $LiteralPath -PathType Leaf)) { throw "missing built executable" }
  Add-Content -LiteralPath $env:TEST_CALLS -Value 'version-metadata'
  if ($env:TEST_METADATA_PRESENT -eq '0') { return [pscustomobject]@{ VersionInfo = $null } }
  $parts = $env:TEST_PRODUCT_VERSION.Split('.')
  return [pscustomobject]@{ VersionInfo = [pscustomobject]@{
    ProductMajorPart = [int]$parts[0]; ProductMinorPart = [int]$parts[1]
    ProductBuildPart = [int]$parts[2]; ProductPrivatePart = [int]$parts[3]
    ProductVersion = 'untrusted display string'
  } }
}
& (Join-Path $env:TEST_REPO 'build/windows/build.ps1') -WorkDir $env:TEST_WORK -Jobs 3 -Resume:($env:TEST_RESUME -eq '1')
''')
        result = subprocess.run([str(PWSH), "-NoProfile", "-File", str(runner)],
                                env=env, capture_output=True, text=True, timeout=20)
        events = calls.read_text().splitlines() if calls.exists() else []
        self.assertNotIn('chrome', events)
        self.assertNotIn('upstream-chrome', events)
        self.assertEqual((out / ".ninja_log").read_text(), "# ninja log v6\n")
        self.assertEqual((out / ".ninja_deps").read_text(), "retained deps")
        self.assertEqual((src / "chrome/browser-fixture.cc").read_text(), "chromium.9oo91esource.qjz9zk\n")
        return result, events, out, src

    def test_direct_restored_builder_finishes_before_bootstrapping_incompatible_gn(self):
        result, events, out, src = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(events, ['inspect', 'prep-patch', 'select', 'bindgen', 'finish', 'bootstrap', 'gn',
                                  'plan', 'evidence-before', 'ninja', 'evidence-after', 'version-metadata'])
        evidence = out.parents[2] / 'upstream-reuse'
        self.assertTrue((evidence / 'baseline.json').is_file())
        self.assertEqual(json.loads((evidence / 'result.json').read_text()), {'exit_code': 0})
        self.assertFalse((out / "changed-compiler.obj").exists())
        self.assertIn("upstream_extra = true", (out / "args.gn").read_text())
        self.assertIn("symbol_level = 0", (out / "args.gn").read_text())
        self.assertEqual((src / "tools/clang/scripts/update.py").read_text(), "# commondatastorage.googleapis.com\n")
        self.assertNotIn("chromium.9oo91esource.qjz9zk", (src / "tools/rust/build_bindgen.py").read_text())
        self.assertFalse((src / "out/Chromix").exists())
        version = load_pins(REPO, "windows")["ChromiumVersion"]
        self.assertIn(f"Windows PE product version verified: {version}", result.stdout)
        self.assertIn("metadata only; not a runtime smoke test", result.stdout)

    def test_present_bindgen_still_finishes_without_endpoint_normalization(self):
        result, events, out, src = self.run_builder(bindgen_present=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(events, ['inspect', 'prep-patch', 'select', 'finish', 'bootstrap', 'gn',
                                  'plan', 'evidence-before', 'ninja', 'evidence-after', 'version-metadata'])
        self.assertIn("commondatastorage.9oo91eapis.qjz9zk", (src / "tools/clang/scripts/update.py").read_text())

    def test_pre_gn_failures_never_execute_gn_or_ninja(self):
        for failure in ('inspect', 'prep-patch', 'select', 'normalize', 'bindgen', 'finish', 'bootstrap'):
            with self.subTest(failure=failure):
                result, events, _, _ = self.run_builder(fail=failure)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse({'gn', 'wrong-gn', 'ninja', 'wrong-ninja', 'version-metadata'} & set(events), events)
                if failure == 'normalize':
                    self.assertNotIn('bindgen', events)
                    self.assertIn('endpoint normalization failed', result.stderr)
                elif failure == 'bindgen':
                    self.assertNotIn('finish', events)
                elif failure == 'finish':
                    self.assertNotIn('bootstrap', events)

    def test_direct_resume_preserves_initial_evidence_baseline(self):
        result, events, out, _ = self.run_builder(resume=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(events[-4:], ['evidence-before', 'ninja', 'evidence-after', 'version-metadata'])
        baseline = out.parents[2] / 'upstream-reuse/baseline.json'
        self.assertEqual(baseline.read_text(), '{"original": true}')
        self.assertEqual(baseline.stat().st_mtime_ns, 1_700_000_000_000_000_000)

    def test_evidence_and_plan_failures_are_fatal_before_version_check(self):
        for failure in ('plan', 'evidence-before', 'evidence-after'):
            with self.subTest(failure=failure):
                result, events, _, _ = self.run_builder(fail=failure)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn('version-metadata', events)
                self.assertEqual(events[-1], failure)
                self.assertEqual('ninja' in events, failure == 'evidence-after')

    def test_failed_ninja_is_recorded_and_not_masked_by_evidence_failure(self):
        for failure in ('', 'evidence-after'):
            with self.subTest(failure=failure):
                result, events, out, _ = self.run_builder(fail=failure, ninja_rc=19)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('ninja failed (exit 19)', result.stderr)
                self.assertEqual(events[-3:], ['evidence-before', 'ninja', 'evidence-after'])
                self.assertNotIn('version-metadata', events)
                if failure:
                    self.assertRegex(result.stderr, r'evidence collection failed after[\s|]+Ninja \(exit 23\)')
                else:
                    self.assertEqual(json.loads((out.parents[2] / 'upstream-reuse/result.json').read_text()),
                                     {'exit_code': 19})

    def test_numeric_product_version_mismatch_rejects_completion(self):
        version = load_pins(REPO, "windows")["ChromiumVersion"]
        pinned = [int(part) for part in version.split(".")]
        versions = [".".join(str(value + (index == changed)) for index, value in enumerate(pinned))
                    for changed in range(4)] + ["0.0.0.0"]
        shared_version = (REPO / "CHROMIUM_VERSION").read_text().strip()
        if shared_version != version:
            versions.append(shared_version)
        for restored in (False, True):
            for version in versions:
                with self.subTest(restored=restored, version=version):
                    result, events, _, _ = self.run_builder(restored=restored, product_version=version)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertRegex(result.stderr, r'does not match the pinned Chromium[\s|]+version')
                    self.assertEqual(events[-1], 'version-metadata')
                    if restored:
                        self.assertEqual(events[-3:], ['ninja', 'evidence-after', 'version-metadata'])
                    self.assertNotIn('product version verified', result.stdout)
                    self.assertNotIn('==> Done:', result.stdout)

    def test_missing_executable_or_version_metadata_rejects_completion(self):
        for restored in (False, True):
            for options, message, checked in (
                    ({"chrome_present": False}, 'built Windows browser is missing', False),
                    ({"metadata_present": False}, 'version metadata is missing', True)):
                with self.subTest(restored=restored, options=options):
                    result, events, _, _ = self.run_builder(restored=restored, **options)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(message, result.stderr)
                    self.assertEqual('version-metadata' in events, checked)
                    if restored:
                        self.assertIn('evidence-after', events)
                    self.assertNotIn('product version verified', result.stdout)
                    self.assertNotIn('==> Done:', result.stdout)

    def test_cold_builder_keeps_chromix_output_and_skips_restored_preparation(self):
        for bindgen_present in (False, True):
            with self.subTest(bindgen_present=bindgen_present):
                result, events, out, src = self.run_builder(restored=False, bindgen_present=bindgen_present)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(events, ['prep-patch'] + ([] if bindgen_present else ['bindgen']) +
                                 ['gn', 'ninja', 'version-metadata'])
                self.assertEqual(out.name, 'Chromix')
                self.assertFalse((out.parents[2] / 'upstream-reuse').exists())
                self.assertNotIn('upstream_extra', (out / 'args.gn').read_text())
                self.assertTrue((out / 'changed-compiler.obj').exists())
                self.assertIn('commondatastorage.9oo91eapis.qjz9zk', (src / 'tools/clang/scripts/update.py').read_text())


if __name__ == "__main__":
    unittest.main()
