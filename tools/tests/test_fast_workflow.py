"""Fast POSIX profiles and bounded single-job builds without downloads or compilation."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github/workflows/build-posix-github.yml"
BASH32 = Path.home() / ".local/bash-3.2-for-ci/bash"
SHELLS = [shutil.which("bash")] + ([str(BASH32)] if BASH32.is_file() else [])


class FastWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(WORKFLOW.read_text())

    def test_reusable_profile_defaults_to_fast_and_staging_remains_default(self):
        events = self.workflow.get("on", self.workflow.get(True))
        inputs = events["workflow_call"]["inputs"]
        self.assertEqual(inputs["build_profile"],
                         {"required": False, "type": "string", "default": "fast"})
        self.assertEqual(inputs["max-stages"]["default"], 8)
        self.assertFalse(self.workflow["concurrency"]["cancel-in-progress"])

    def test_caller_workflows_have_independent_concurrency_groups(self):
        group = self.workflow["concurrency"]["group"]
        self.assertEqual(group, "build-posix-${{ github.workflow }}-${{ inputs.platform }}-"
                               "${{ inputs.arch }}-${{ github.ref }}")
        for platform in ("linux", "macos"):
            for arch in ("x64", "arm64"):
                def resolve(caller):
                    return (group.replace("${{ github.workflow }}", caller)
                            .replace("${{ inputs.platform }}", platform)
                            .replace("${{ inputs.arch }}", arch)
                            .replace("${{ github.ref }}", "refs/heads/main"))
                standalone = resolve(f"build-{platform}-{arch}")
                self.assertNotEqual(standalone, resolve("build-cross-platform"))
                self.assertNotEqual(standalone, f"build-{platform}-{arch}-refs/heads/main")
                self.assertNotEqual(standalone, f"build-posix-{platform}-{arch}-refs/heads/main")

    def test_every_stage_passes_profile_reserve_and_exact_stage_arguments(self):
        for index in range(1, 9):
            with self.subTest(stage=index):
                job = self.workflow["jobs"][f"posix-{index}"]
                stage = next(step for step in job["steps"] if step.get("id") == "stage")
                self.assertEqual(stage["env"]["CHROMIX_BUILD_PROFILE"], "${{ inputs.build_profile }}")
                self.assertEqual(stage["env"]["CHROMIX_RESERVE_MINUTES"],
                                 "${{ inputs.platform == 'macos' && '90' || inputs['max-stages'] == 1 && '15' || '45' }}")
                self.assertEqual(job["timeout-minutes"], 355)
                self.assertNotIn("continue-on-error", stage)
                self.assertNotIn("continue-on-error", job)
                self.assertIn(f"--stage-index {index} --max-stages "
                              "'${{ inputs['max-stages'] }}'", stage["run"])
                self.assertIn('--deadline-epoch "$DEADLINE_EPOCH"', stage["run"])
                if index == 1:
                    self.assertNotIn("--from-snapshot", stage["run"])
                    self.assertNotIn("if", job)
                else:
                    self.assertIn(f"inputs['max-stages'] >= {index}", job["if"])
                    self.assertIn("always()", job["if"])
                    self.assertEqual(job["needs"], f"posix-{index - 1}")
                    self.assertIn(f"needs.posix-{index - 1}.result == 'success'", job["if"])
                    self.assertIn(f"needs.posix-{index - 1}.outputs.finished != 'true'", job["if"])
                    self.assertIn('--from-snapshot "${RUNNER_TEMP}/chromix-restore"', stage["run"])
                final = next(step for step in job["steps"] if step.get("name") == "Upload final bundle")
                self.assertEqual(final["if"], "steps.stage.outputs.finished == 'true'")
                self.assertEqual(final["with"]["if-no-files-found"], "error")
                diagnostics = next(step for step in job["steps"]
                                   if step.get("name") == "Upload build diagnostics")
                self.assertEqual(diagnostics["if"], "always()")

    def test_each_deadline_is_the_earlier_stage_or_job_limit(self):
        start = 1700000000
        for index in range(1, 9):
            job = self.workflow["jobs"][f"posix-{index}"]
            stage = next(step for step in job["steps"] if step.get("id") == "stage")
            deadline_script = stage["run"].split("build/posix/ci-stage.sh", 1)[0]
            for shell in SHELLS:
                for elapsed in (0, 10, 30, 90, 329, 340):
                    with self.subTest(stage=index, shell=shell, elapsed=elapsed):
                        now = start + elapsed * 60
                        result = subprocess.run(
                            [shell, "--norc", "-c",
                             'date() { printf "%s\\n" "$NOW_FIXTURE"; }\n'
                             + deadline_script + 'printf "%s\\n" "$DEADLINE_EPOCH"\n'],
                            env={**os.environ, "CHROMIX_JOB_START_EPOCH": str(start),
                                 "NOW_FIXTURE": str(now)},
                            capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        deadline = int(result.stdout.strip())
                        self.assertEqual(deadline, min(now + 300 * 60, start + 330 * 60))
                        self.assertGreaterEqual(start + job["timeout-minutes"] * 60 - deadline, 25 * 60)

    def test_resource_record_is_first_and_exports_start_before_setup(self):
        for index in range(1, 9):
            steps = self.workflow["jobs"][f"posix-{index}"]["steps"]
            self.assertEqual(steps[0]["name"], "Record runner resources")
            self.assertEqual(steps[1]["uses"], "actions/checkout@v4")
            source = steps[0]["run"]
            self.assertLess(source.index("CHROMIX_JOB_START_EPOCH="), source.index("mkdir"))
            self.assertIn('>> "$GITHUB_ENV"', source)
            self.assertIn("uname -a", source)
            self.assertIn("df -h", source)
            self.assertIn("getconf _NPROCESSORS_ONLN", source)
            self.assertIn("free -h", source)
            self.assertIn("sysctl hw.ncpu hw.memsize", source)
            self.assertNotIn("swapon", source)

    def test_resource_record_uses_only_host_supported_commands(self):
        source = self.workflow["jobs"]["posix-1"]["steps"][0]["run"]
        stubs = '''date() { printf '1700000000\\n'; }
uname() { printf '%s\\n' "$HOST_FIXTURE"; }
df() { printf 'disk fixture\\n'; }
getconf() { test "$HOST_FIXTURE" = Linux || return 1; printf 'CPU fixture\\n'; }
free() { test "$HOST_FIXTURE" = Linux || return 1; printf 'RAM fixture\\n'; }
sysctl() { test "$HOST_FIXTURE" = Darwin || return 1; printf 'CPU fixture\\nRAM fixture\\n'; }
'''
        for shell in SHELLS:
            for host in ("Linux", "Darwin"):
                with self.subTest(shell=shell, host=host), tempfile.TemporaryDirectory() as temp:
                    output = Path(temp) / "github_env"
                    result = subprocess.run(
                        [shell, "--norc", "-c", stubs + source],
                        env={**os.environ, "HOST_FIXTURE": host, "RUNNER_TEMP": temp,
                             "GITHUB_ENV": str(output)},
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(output.read_text(), "CHROMIX_JOB_START_EPOCH=1700000000\n")
                    log = (Path(temp) / "chromix-logs/runner.log").read_text()
                    for value in (host, "CPU fixture", "RAM fixture", "disk fixture"):
                        self.assertIn(value, log)


class FastBuildProfileTest(unittest.TestCase):
    def test_generic_merge_is_unchanged_without_explicit_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second, output = (root / name for name in ("first.gn", "second.gn", "out.gn"))
            first.write_text("is_official_build = true\nsymbol_level = 2\n")
            second.write_text("symbol_level = 0\n")
            env = {**os.environ, "CHROMIX_BUILD_PROFILE": "fast"}
            command = [sys.executable, str(REPO / "tools/merge_gn_args.py"),
                       str(output), str(first), str(second)]
            subprocess.run(command, env=env, check=True, capture_output=True, timeout=10)
            self.assertNotIn("thin_lto_enable_optimizations", output.read_text())
            self.assertIn("symbol_level = 0", output.read_text())
            first.write_text(first.read_text() + "thin_lto_enable_optimizations = true\n")
            subprocess.run(command, env=env, check=True, capture_output=True, timeout=10)
            self.assertIn("thin_lto_enable_optimizations = true", output.read_text())

    def test_profile_switch_overrides_restored_args_in_place(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "args.gn"
            output.write_text("is_official_build = true\n")
            for profile in ("fast", "release", "fast"):
                command = [sys.executable, str(REPO / "tools/merge_gn_args.py"),
                           "--build-profile", profile, str(output), str(output)]
                subprocess.run(command, check=True, capture_output=True, timeout=10)
                text = output.read_text()
                self.assertEqual(text.count("thin_lto_enable_optimizations ="), 1)
                self.assertIn("thin_lto_enable_optimizations = " +
                              ("false" if profile == "fast" else "true"), text)
            before = output.read_bytes()
            result = subprocess.run([sys.executable, str(REPO / "tools/merge_gn_args.py"),
                                     "--build-profile", "typo", str(output), str(output)],
                                    capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), before)

    def test_only_posix_builders_explicitly_forward_profile(self):
        for filename in ("build/build.sh", "build/macos/build.sh"):
            source = (REPO / filename).read_text()
            self.assertIn('BUILD_PROFILE="${CHROMIX_BUILD_PROFILE:-release}"', source)
            self.assertIn('--build-profile "$BUILD_PROFILE"', source)
            for shell in SHELLS:
                with self.subTest(filename=filename, shell=shell), tempfile.TemporaryDirectory() as temp:
                    work = Path(temp) / "unused"
                    result = subprocess.run(
                        [shell, str(REPO / filename), str(work), "x64"],
                        env={**os.environ, "CHROMIX_BUILD_PROFILE": "typo"},
                        capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("CHROMIX_BUILD_PROFILE must be fast or release", result.stderr)
                    self.assertFalse(work.exists())
        self.assertNotIn("--build-profile", (REPO / "build/windows/build.ps1").read_text())
        self.assertIn('@("fast", "release")', (REPO / "build/windows/ci-stage.ps1").read_text())

    def test_fast_and_release_differ_only_in_thinlto_optimization(self):
        for platform, filename in (("linux", "args.gn"), ("macos", "args.macos.gn")):
            for arch in ("x64", "arm64"):
                with self.subTest(platform=platform, arch=arch), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    upstream = root / "upstream.gn"
                    upstream.write_text(
                        "use_thin_lto = true\nis_cfi = true\nuse_cfi_icall = true\n"
                        "v8_enable_sandbox = true\nenable_rust = true\n"
                        "enable_extensions = true\nenable_pdf = true\n"
                        "enable_webrtc = true\nuse_dawn = true\n"
                        "thin_lto_enable_optimizations = true\n")
                    target = root / "target.gn"
                    target.write_text(f'target_cpu = "{arch}"\n')
                    merged = {}
                    for profile in ("fast", "release"):
                        if profile == "release":
                            upstream.write_text(upstream.read_text().replace(
                                "thin_lto_enable_optimizations = true", "thin_lto_enable_optimizations = false"))
                        output = root / f"{profile}.gn"
                        command = [sys.executable, str(REPO / "tools/merge_gn_args.py"),
                                   "--build-profile", profile, str(output), str(upstream),
                                   str(REPO / "build" / filename), str(target)]
                        result = subprocess.run(command, env={**os.environ, "CHROMIX_BUILD_PROFILE": profile},
                                                capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        before = output.read_bytes()
                        subprocess.run(command, env={**os.environ, "CHROMIX_BUILD_PROFILE": profile},
                                       check=True, capture_output=True, timeout=10)
                        self.assertEqual(output.read_bytes(), before)
                        assignments = [line.strip() for line in output.read_text().splitlines()
                                       if line.strip() and not line.lstrip().startswith("#")]
                        values = dict(tuple(part.strip() for part in line.split("=", 1))
                                      for line in assignments)
                        self.assertEqual(len(assignments), len(values))
                        merged[profile] = values
                        self.assertEqual(values["thin_lto_enable_optimizations"],
                                         "false" if profile == "fast" else "true")
                        for key in ("use_thin_lto", "is_cfi", "use_cfi_icall", "v8_enable_sandbox",
                                    "enable_rust", "enable_extensions", "enable_pdf", "enable_webrtc",
                                    "use_dawn", "is_official_build", "proprietary_codecs", "enable_widevine"):
                            self.assertEqual(values[key], "true", key)
                        self.assertEqual(values["ffmpeg_branding"], '"Chrome"')
                        self.assertEqual(values["is_debug"], "false")
                        self.assertEqual(values["is_component_build"], "false")
                        self.assertEqual(values["target_cpu"], f'"{arch}"')
                    self.assertEqual({key for key in merged["fast"].keys() | merged["release"].keys()
                                      if merged["fast"].get(key) != merged["release"].get(key)},
                                     {"thin_lto_enable_optimizations"})


class SingleStageCompletionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="single stage ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.work = self.root / "work"
        self.output = self.root / "outputs"
        self.stage = self.repo / "build/posix/ci-stage.sh"
        self.put(self.stage, (REPO / "build/posix/ci-stage.sh").read_text())
        self.put(self.repo / "build/prepare-ungoogled.sh", "#!/bin/sh\nexit 99\n")
        self.put(self.repo / "build/build.sh", "#!/bin/sh\nexit 0\n")
        self.put(self.repo / "build/macos/build.sh", "#!/bin/sh\nexit 0\n")
        self.put(self.repo / "build/posix/ci-parts.sh",
                 '#!/bin/sh\nmkdir -p "$2/p1"\nprintf snapshot > "$2/p1/tree.tar.zst.000"\n')
        binaries = self.root / "bin"
        self.put(binaries / "date", "#!/bin/sh\nprintf '1700000000\\n'\n")
        self.put(binaries / "timeout", '#!/bin/sh\nexit "$BUILD_EXIT_FIXTURE"\n')
        self.env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                    "FIXTURE_BIN": binaries.as_posix(),
                    "CHROMIX_USE_UPSTREAM_CACHE": "0", "CHROMIX_BUILD_PROFILE": "fast",
                    "CHROMIX_RESERVE_MINUTES": "15", "BUILD_EXIT_FIXTURE": "124",
                    "GITHUB_OUTPUT": str(self.output)}

    def put(self, path, source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, newline="\n")
        path.chmod(0o755)

    def run_stage(self, shell, platform, arch, stage, maximum, phase):
        if self.work.exists():
            shutil.rmtree(self.work)
        self.work.mkdir()
        self.output.write_text("")
        if phase != "prepare":
            self.put(self.work / "src/.chromix-source-ready", "fixture\n")
        minutes = 30 if phase in ("prepare", "ninja") else 300
        return subprocess.run(
            [shell, "--norc", "-c",
             'export PATH="$(cd "$FIXTURE_BIN" && pwd):$PATH"; exec "$BASH" "$@"',
             "fixture", str(self.stage), "--platform", platform, "--arch", arch,
             "--workdir", str(self.work), "--stage-index", str(stage), "--max-stages", str(maximum),
             "--deadline-epoch", str(1700000000 + minutes * 60)],
            env=self.env, capture_output=True, text=True, timeout=10)

    def test_invalid_stage_limits_and_profile_fail_before_work_or_snapshot(self):
        cases = (("0", "1", "fast"), ("1", "0", "fast"), ("1", "9", "fast"),
                 ("2", "1", "fast"), ("1", "1.5", "fast"), ("1", "", "fast"),
                 ("1", "1", "typo"))
        for shell in SHELLS:
            for stage, maximum, profile in cases:
                with self.subTest(shell=shell, stage=stage, maximum=maximum, profile=profile):
                    work = self.root / "invalid-work"
                    result = subprocess.run(
                        [shell, str(self.stage), "--platform", "linux", "--arch", "x64",
                         "--workdir", str(work), "--stage-index", stage, "--max-stages", maximum],
                        env={**self.env, "CHROMIX_BUILD_PROFILE": profile},
                        capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(work.exists())
                    self.assertFalse(self.output.exists())

    def test_exhausted_stages_save_checkpoint_without_reporting_completion(self):
        for shell in SHELLS:
            for platform, arch in (("linux", "x64"), ("linux", "arm64"),
                                   ("macos", "x64"), ("macos", "arm64")):
                for maximum in (1, 8):
                    for phase in ("prepare", "ninja", "timeout"):
                        with self.subTest(shell=shell, platform=platform, arch=arch,
                                          maximum=maximum, phase=phase):
                            result = self.run_stage(shell, platform, arch, maximum, maximum, phase)
                            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                            self.assertIn("without finishing", result.stderr)
                            self.assertTrue(list(self.work.glob(".snapshot-stage-*")))
                            self.assertIn("upload_snapshot=true", self.output.read_text())
                            self.assertNotIn("finished=true", self.output.read_text())

    def test_staged_mode_still_hands_off_when_another_stage_is_available(self):
        for shell in SHELLS:
            for phase in ("prepare", "ninja", "timeout"):
                with self.subTest(shell=shell, phase=phase):
                    result = self.run_stage(shell, "linux", "x64", 1, 8, phase)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertTrue(list(self.work.glob(".snapshot-stage-*")))
                    self.assertIn("upload_snapshot=true", self.output.read_text())
                    self.assertNotIn("finished=true", self.output.read_text())

    def test_single_compile_exit_zero_without_bundle_still_fails(self):
        self.env["BUILD_EXIT_FIXTURE"] = "0"
        for platform, arch in (("linux", "arm64"), ("macos", "x64")):
            with self.subTest(platform=platform, arch=arch):
                result = self.run_stage(SHELLS[0], platform, arch, 1, 1, "missing-bundle")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("is missing", result.stderr)
                self.assertNotIn("finished=true", self.output.read_text())
                self.assertFalse(list(self.work.glob(".snapshot-stage-*")))


if __name__ == "__main__":
    unittest.main()
