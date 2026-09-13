"""Regression tests for the upstream-modeled POSIX staged CI scripts and workflow."""
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))

CI_STAGE = REPO / "build" / "posix" / "ci-stage.sh"
CI_PARTS = REPO / "build" / "posix" / "ci-parts.sh"
GEN_WORKFLOW = REPO / "tools" / "gen_posix_workflow.py"
WORKFLOW = REPO / ".github" / "workflows" / "build-posix-github.yml"
ENTRY_WORKFLOWS = [REPO / '.github/workflows' / f'build-{platform}-{arch}.yml'
                   for platform in ('linux', 'macos') for arch in ('x64', 'arm64')]
BASH32 = Path(os.path.expanduser("~/.local/bash-3.2-for-ci/bash"))


class PosixStageSyntaxTest(unittest.TestCase):
    def test_ci_scripts_are_executable_and_parse(self):
        for script in (CI_STAGE, CI_PARTS):
            self.assertTrue(script.is_file(), script)
            self.assertEqual(os.stat(script).st_mode & stat.S_IXUSR, stat.S_IXUSR)
            result = subprocess.run(["bash", "-n", str(script)],
                                     capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


class PosixStageHandoffExecutionTest(unittest.TestCase):
    """Run the real stage script with isolated build, timeout, and snapshot stubs."""

    SHELL = shutil.which("bash")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chromix handoff ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.work = self.root / "work"
        self.output = self.root / "outputs"
        self.calls = self.root / "calls"
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        self.stage = self.repo / "build/posix/ci-stage.sh"
        self.put(self.stage, CI_STAGE.read_text())
        for name in ("mkdir", "cp", "cat", "env", "dirname"):
            (self.bindir / name).symlink_to(shutil.which(name))
        (self.bindir / "bash").symlink_to(self.SHELL)
        self.put(self.bindir / "date",
                 '#!/bin/sh\n'
                 'if [ -f "$CALL_LOG.expired" ]; then\n'
                 '  printf "1700099999\\n"\n'
                 'else\n'
                 '  printf "1700000000\\n"\n'
                 'fi\n')
        for platform, builder in (("linux", "build/build.sh"),
                                  ("macos", "build/macos/build.sh")):
            self.put(self.repo / builder,
                     '#!/bin/sh\n'
                     f'printf "build {platform} %s %s\\n" "$2" "$CHROMIX_BUILD_PROFILE" >> "$CALL_LOG"\n'
                     'printf "compile progress\\n" > "$1/checkpoint"\n'
                     ': > "$CALL_LOG.expired"\n'
                     'exit "$BUILD_RC"\n')
        self.put(self.repo / "build/prepare-ungoogled.sh", "#!/bin/sh\nexit 99\n")
        self.put(self.repo / "build/posix/ci-parts.sh",
                 '#!/bin/sh\nset -eu\n'
                 'printf "snapshot\\n" >> "$CALL_LOG"\n'
                 'cat "$GITHUB_OUTPUT" > "$CALL_LOG.snapshot-outputs"\n'
                 'printf "%s\\n" "$1" "$2" > "$CALL_LOG.snapshot-args"\n'
                 '[ -d "$2" ]\n'
                 'mkdir -p "$2/p1"\n'
                 'cp "$1/checkpoint" "$2/p1/tree.tar.zst.001"\n'
                 'exit "$SNAPSHOT_RC"\n')

    def put(self, path, source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        path.chmod(0o755)

    def configurations(self):
        for platform in ("linux", "macos"):
            for arch in ("x64", "arm64"):
                for profile in ("fast", "release"):
                    yield {"platform": platform, "arch": arch, "profile": profile}

    def run_stage(self, platform, arch, profile, stage, maximum=8,
                  phase="timeout", build_rc=0, snapshot_rc=0):
        if self.work.exists():
            shutil.rmtree(self.work)
        self.work.mkdir()
        (self.work / "checkpoint").write_text("existing progress\n")
        if phase != "prepare":
            self.put(self.work / "src/.chromix-source-ready", "fixture\n")
        self.output.write_text("")
        self.calls.write_text("")
        for path in self.root.glob("calls.*"):
            path.unlink()
        for name in ("timeout", "gtimeout"):
            path = self.bindir / name
            if path.exists():
                path.unlink()
        timeout_name = "gtimeout" if platform == "macos" else "timeout"
        self.put(self.bindir / timeout_name,
                 '#!/bin/sh\n'
                 'printf "timeout\\n" >> "$CALL_LOG"\n'
                 'printf "%s\\n" "$@" > "$CALL_LOG.timeout-args"\n'
                 'shift 5\n'
                 '"$@"\n'
                 'rc=$?\n'
                 '[ "$rc" -eq 0 ] || exit "$rc"\n'
                 'exit 124\n')
        minutes = 60 if phase in ("prepare", "ninja") else 300
        env = {**os.environ, "FIXTURE_BIN": str(self.bindir),
               "CHROMIX_USE_UPSTREAM_CACHE": "0", "CHROMIX_BUILD_PROFILE": profile,
               "CHROMIX_RESERVE_MINUTES": "45", "GITHUB_OUTPUT": str(self.output),
               "CALL_LOG": str(self.calls), "BUILD_RC": str(build_rc),
               "SNAPSHOT_RC": str(snapshot_rc)}
        result = subprocess.run(
            [str(self.SHELL), "--norc", "-c",
             'export PATH="$FIXTURE_BIN"; exec "$BASH" "$@"', "fixture",
             str(self.stage), "--platform", platform, "--arch", arch,
             "--workdir", str(self.work), "--stage-index", str(stage),
             "--max-stages", str(maximum),
             "--deadline-epoch", str(1700000000 + minutes * 60)],
            env=env, capture_output=True, text=True, timeout=10)
        return result

    def assert_unfinished(self, upload=False):
        expected = ["status=running", "finished=false"]
        if upload:
            expected.append("upload_snapshot=true")
        self.assertEqual(self.output.read_text().splitlines(), expected)
        self.assertFalse((self.work / "dist").exists())

    def assert_checkpoint(self, stage, contents):
        snapshot = self.work / f".snapshot-stage-{stage}"
        self.assertEqual((snapshot / "p1/tree.tar.zst.001").read_text(), contents)
        self.assertEqual((self.root / "calls.snapshot-args").read_text().splitlines(),
                         [str(self.work), str(snapshot)])
        self.assertEqual((self.root / "calls.snapshot-outputs").read_text().splitlines(),
                         ["status=running", "finished=false"])

    def test_final_timeout_preserves_checkpoint_but_fails(self):
        for config in self.configurations():
            for maximum in (1, 8):
                with self.subTest(**config, maximum=maximum):
                    result = self.run_stage(**config, stage=maximum, maximum=maximum)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("deadline reached after 255m", result.stderr)
                    self.assertIn(f"stage {maximum} reached max-stages {maximum} without finishing",
                                  result.stderr)
                    self.assertEqual(self.calls.read_text().splitlines(),
                                     ["timeout", f"build {config['platform']} {config['arch']} {config['profile']}",
                                      "snapshot"])
                    self.assertEqual((self.root / "calls.timeout-args").read_text().splitlines()[:5],
                                     ["-k", "7m", "-s", "SIGTERM", "255m"])
                    self.assert_checkpoint(maximum, "compile progress\n")
                    self.assert_unfinished(upload=True)

    def test_earlier_timeout_keeps_successful_unfinished_handoff(self):
        for config in self.configurations():
            for stage in (1, 7):
                with self.subTest(**config, stage=stage):
                    result = self.run_stage(**config, stage=stage)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("deadline reached after 255m", result.stderr)
                    self.assertNotIn("without finishing", result.stderr)
                    self.assertEqual(self.calls.read_text().splitlines(),
                                     ["timeout", f"build {config['platform']} {config['arch']} {config['profile']}",
                                      "snapshot"])
                    self.assert_checkpoint(stage, "compile progress\n")
                    self.assert_unfinished(upload=True)

    def test_low_budget_handoffs_preserve_checkpoints(self):
        for config in self.configurations():
            for stage, maximum in ((1, 8), (8, 8), (1, 1)):
                for phase in ("prepare", "ninja"):
                    with self.subTest(**config, stage=stage, maximum=maximum, phase=phase):
                        result = self.run_stage(**config, stage=stage, maximum=maximum, phase=phase)
                        self.assertEqual(result.returncode, int(stage == maximum), result.stderr)
                        self.assertIn("below minimum", result.stderr)
                        self.assertEqual(self.calls.read_text().splitlines(), ["snapshot"])
                        self.assert_checkpoint(stage, "existing progress\n")
                        self.assert_unfinished(upload=True)

    def test_compiler_failures_do_not_become_deadlines(self):
        for config in self.configurations():
            for stage in (1, 8):
                for rc in (1, 2, 137):
                    with self.subTest(**config, stage=stage, rc=rc):
                        result = self.run_stage(**config, stage=stage, build_rc=rc)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(f"build script failed at stage {stage} (exit {rc})", result.stderr)
                        self.assertNotIn("deadline reached", result.stderr)
                        self.assertNotIn("without finishing", result.stderr)
                        # The stub clock is past the deadline after the compiler exits.
                        self.assertTrue((self.root / "calls.expired").is_file())
                        self.assertEqual(self.calls.read_text().splitlines(),
                                         ["timeout", f"build {config['platform']} {config['arch']} {config['profile']}"])
                        self.assertFalse(list(self.work.glob(".snapshot-stage-*")))
                        self.assert_unfinished()

    def test_snapshot_failure_never_emits_upload_marker(self):
        for config in self.configurations():
            for stage, maximum in ((1, 8), (8, 8), (1, 1)):
                with self.subTest(**config, stage=stage, maximum=maximum):
                    result = self.run_stage(**config, stage=stage, maximum=maximum, snapshot_rc=23)
                    self.assertEqual(result.returncode, 23, result.stderr)
                    self.assertEqual(self.calls.read_text().splitlines()[-1], "snapshot")
                    self.assertNotIn("without finishing", result.stderr)
                    self.assert_checkpoint(stage, "compile progress\n")
                    self.assert_unfinished()


@unittest.skipUnless(BASH32.is_file(), "locally built bash 3.2 required")
class PosixStageHandoffBash32ExecutionTest(PosixStageHandoffExecutionTest):
    SHELL = str(BASH32)


@unittest.skipUnless(shutil.which("zstd"), "zstd required")
class PosixSnapshotRoundTripTest(unittest.TestCase):
    """ci-parts.sh must pack tar|zstd volumes that ci-stage.sh can restore."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromix posix ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.fixture_files = {
            "src/.chromix-source-ready":
                "linux|x64|152.0.7977.82|core|platform|patchhash\n",
            "src/.chromix-ungoogled-core": "core-commit\n",
            "src/.chromix-ungoogled-platform": "platform-commit\n",
            "src/.chromix-chromium-version": "152.0.7977.82\n",
            "src/fixture.c": '#include "fixture.h"\nint fixture(void) { return VALUE; }\n',
            "src/fixture.h": "#define VALUE 42\n",
            "src/out/Chromix/obj/fixture.o": "object fixture\n",
            "src/out/Chromix/.ninja_log": "# ninja log v5\n",
            "src/out/Chromix/.ninja_deps": "dependency fixture\n",
            "src/tool": "#!/bin/sh\necho ok\n",
            "tooling/depot_tools/gclient": "tooling fixture\n",
            "src/download_cache/keep": "not the root cache\n",
            "src/dist/keep": "not the root package\n",
            "src/smoke/keep": "not the root smoke profile\n",
            "src/runtime-smoke-stage-1/keep": "not the root smoke evidence\n",
            "tooling/handoff/custom parts/keep": "not the parts directory\n",
            "tooling/handoff/custom [1]*?\\tail/keep": "literal nested path\n",
            ".root-marker": "hidden root marker\n",
            "-C": "not a tar option\n",
            "line\nbreak": "NUL-delimited name\n",
            "link-target": "fixture",
        }
        self.mtime_ns = 1700000000123456789
        for name, contents in self.fixture_files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
            path.chmod(0o755 if name == "src/tool" else 0o640)
        self.fixture_symlinks = {
            "src/symlink": "../link-target",
            "tooling/download_cache": "../download_cache",
            "broken-link": "missing-target",
            "src/out-link": "out/Chromix",
        }
        for name, target in self.fixture_symlinks.items():
            (self.work / name).symlink_to(target)
        self.fixture_directories = []
        self.mtimes_ns = {}
        for index, path in enumerate([self.work, *sorted(self.work.rglob("*"))]):
            name = str(path.relative_to(self.work))
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o750)
                if path != self.work:
                    self.fixture_directories.append(name)
            mtime_ns = self.mtime_ns + index * 1000001
            os.utime(path, ns=(mtime_ns, mtime_ns), follow_symlinks=False)
            self.assertEqual(path.lstat().st_mtime_ns, mtime_ns,
                             "snapshot tests require nanosecond filesystem timestamps")
            self.mtimes_ns[name] = mtime_ns
        # A small volume size forces the multi-volume slicing path in tests.
        self.env = {
            **os.environ,
            "CHROMIX_SNAPSHOT_VOLUME_BYTES": str(64 * 1024),
            "CHROMIX_SNAPSHOT_MAX_VOLUMES": "8",
        }

    def round_trip_payload(self, repeat):
        # Random bytes defeat zstd's compression, so volume counts follow
        # CHROMIX_SNAPSHOT_VOLUME_BYTES exactly as they do in production.
        rng = __import__("random").Random(0)
        return bytes(rng.getrandbits(8) for _ in range(64 * 1024 * repeat))

    def snapshot(self, extra_env=None, parts_dir=None, shell="bash", work=None):
        if parts_dir is None:
            parts_dir = self.root / f"parts-{len(list(self.root.iterdir()))}"
        result = subprocess.run(
            [str(shell), str(CI_PARTS), str(work or self.work), str(parts_dir)],
            capture_output=True, text=True, cwd=self.root, timeout=60,
            env={**self.env, **(extra_env or {})})
        return parts_dir, result

    def snapshot_tar(self, parts_dir):
        # Volumes only compare correctly by their numeric suffix, not slot.
        archives = sorted(
            (p for p in parts_dir.rglob("tree.tar.zst.*")
             if p.name != "tree.tar.zst."),
            key=lambda p: int(p.name.rsplit(".", 1)[1]))
        self.assertTrue(archives)
        decoded = subprocess.run(
            ["zstd", "-d", "-T0"],
            input=b"".join(p.read_bytes() for p in archives),
            capture_output=True, timeout=60)
        self.assertEqual(decoded.returncode, 0, decoded.stderr)
        return decoded.stdout

    def restore(self, parts_dir, dest, extra_env=None):
        extract = subprocess.run(
            ["tar", "-xpf", "-", "-C", str(dest)],
            input=self.snapshot_tar(parts_dir), capture_output=True, timeout=60,
            env={**self.env, **(extra_env or {})})
        self.assertEqual(extract.returncode, 0, extract.stderr)

    def assert_fixture(self, dest):
        for name, contents in self.fixture_files.items():
            with self.subTest(path=name):
                path = dest / name
                self.assertEqual(path.read_text(), contents)
                expected_mode = 0o755 if name == "src/tool" else 0o640
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected_mode)
                self.assertEqual(path.stat().st_mtime_ns, self.mtimes_ns[name])
        for name, target in self.fixture_symlinks.items():
            self.assertTrue((dest / name).is_symlink(), name)
            self.assertEqual(os.readlink(dest / name), target)
            self.assertEqual((dest / name).lstat().st_mtime_ns, self.mtimes_ns[name])
            self.assertEqual(stat.S_IMODE((dest / name).lstat().st_mode),
                             stat.S_IMODE((self.work / name).lstat().st_mode))
        self.assertEqual((dest / "src/symlink").read_text(), "fixture")
        for directory in self.fixture_directories:
            self.assertEqual((dest / directory).stat().st_mtime_ns,
                             (self.work / directory).stat().st_mtime_ns, directory)
            self.assertEqual(stat.S_IMODE((dest / directory).stat().st_mode), 0o750)

    def archive_mtime_ns(self, member):
        from decimal import Decimal
        self.assertIn("mtime", member.pax_headers, member.name)
        return int(Decimal(member.pax_headers["mtime"]) * 1000000000)

    def tar_environment(self, implementation):
        for candidate in ("tar", "gtar", "bsdtar"):
            executable = shutil.which(candidate)
            if executable is None:
                continue
            version = subprocess.run([executable, "--version"],
                                     capture_output=True, text=True, timeout=10)
            signature = {"gnu": "GNU tar", "bsd": "bsdtar"}[implementation]
            if version.returncode == 0 and signature in version.stdout:
                bindir = self.root / f"{implementation}-bin"
                bindir.mkdir(exist_ok=True)
                if not (bindir / "tar").exists():
                    (bindir / "tar").symlink_to(executable)
                return {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
        self.skipTest(f"{implementation.upper()} tar required")

    def tree_metadata(self, work):
        return {
            str(path.relative_to(work)): (path.lstat().st_mode, path.lstat().st_mtime_ns)
            for path in [work, *work.rglob("*")]
        }

    def assert_tree_metadata(self, dest, expected):
        for name, metadata in expected.items():
            with self.subTest(path=name):
                path = dest / name
                self.assertEqual((path.lstat().st_mode, path.lstat().st_mtime_ns),
                                 metadata)

    def nanosecond_round_trip(self, writer, reader):
        writer_env = self.tar_environment(writer)
        reader_env = self.tar_environment(reader)
        expected = self.tree_metadata(self.work)
        parts, result = self.snapshot(writer_env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with tarfile.open(fileobj=io.BytesIO(self.snapshot_tar(parts))) as tree:
            for member in tree.getmembers():
                name = str(Path(member.name))
                self.assertEqual(self.archive_mtime_ns(member), expected[name][1], name)
        restored = self.root / "restored"
        restored.mkdir()
        for attempt in range(2):
            with self.subTest(restore=attempt):
                self.restore(parts, restored, reader_env)
                self.assert_fixture(restored)
                self.assertEqual(self.tree_metadata(restored), expected)
                # Re-restores must repair existing metadata, including symlinks.
                for name in expected:
                    os.utime(restored / name, ns=(self.mtime_ns - 7, self.mtime_ns - 7),
                             follow_symlinks=False)

    def test_gnu_to_gnu_preserves_nanosecond_metadata(self):
        self.nanosecond_round_trip("gnu", "gnu")

    def test_bsd_to_bsd_preserves_nanosecond_metadata(self):
        self.nanosecond_round_trip("bsd", "bsd")

    def test_gnu_to_bsd_preserves_nanosecond_metadata(self):
        self.nanosecond_round_trip("gnu", "bsd")

    def test_bsd_to_gnu_preserves_nanosecond_metadata(self):
        self.nanosecond_round_trip("bsd", "gnu")

    def ninja_round_trip(self, writer, reader):
        import shlex
        writer_env = self.tar_environment(writer)
        reader_env = self.tar_environment(reader)
        ninja = shutil.which("ninja")
        compiler = shutil.which("cc")
        if ninja is None or compiler is None:
            self.skipTest("Ninja and a C compiler required")
        out = self.work / "src/out/Chromix"
        for name in ("obj/fixture.o", ".ninja_log", ".ninja_deps"):
            (out / name).unlink()
        object_mtime_ns = self.mtime_ns + 500000000
        stamp = (f'import os; os.utime("obj/fixture.o", '
                 f'ns=({object_mtime_ns}, {object_mtime_ns}))')
        (out / "build.ninja").write_text(
            "rule cc\n"
            f"  command = {shlex.quote(compiler)} -MMD -MF $out.d -c $in -o $out && "
            f"{shlex.quote(sys.executable)} -c {shlex.quote(stamp)}\n"
            "  depfile = $out.d\n"
            "  deps = gcc\n"
            "build obj/fixture.o: cc ../../fixture.c\n"
            "default obj/fixture.o\n")

        def run_ninja(directory, *args):
            result = subprocess.run([ninja, *args], cwd=directory,
                                    capture_output=True, text=True, timeout=60,
                                    env={**self.env, "LC_ALL": "C"})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout + result.stderr

        self.assertIn("[1/1]", run_ninja(out))
        self.assertIn("../../fixture.h", run_ninja(out, "-t", "deps"))
        self.assertFalse((out / "obj/fixture.o.d").exists())
        # The deps database keeps the object's ns timestamp independently of tar.
        for index, name in enumerate((".ninja_log", ".ninja_deps", "build.ninja")):
            mtime_ns = self.mtime_ns + 600000000 + index * 1000001
            os.utime(out / name, ns=(mtime_ns, mtime_ns))
        for index, directory in enumerate([self.work, *self.work.rglob("*")]):
            if directory.is_dir() and not directory.is_symlink():
                mtime_ns = self.mtime_ns + 700000000 + index * 1000001
                os.utime(directory, ns=(mtime_ns, mtime_ns))
        self.assertIn("ninja: no work to do.", run_ninja(out, "-d", "explain"))
        expected = self.tree_metadata(self.work)
        contents = {name: (self.work / name).read_bytes() for name in expected
                    if stat.S_ISREG(expected[name][0])}
        for name, (_, mtime_ns) in expected.items():
            self.assertNotEqual(mtime_ns % 1000000000, 0, name)
        parts, result = self.snapshot(writer_env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored = self.root / "ninja-restored"
        restored.mkdir()
        for attempt in range(2):
            with self.subTest(restore=attempt):
                self.restore(parts, restored, reader_env)
                restored_out = restored / "src/out/Chromix"
                self.assertIn("ninja: no work to do.",
                              run_ninja(restored_out, "-d", "explain"))
                self.assertEqual(self.tree_metadata(restored), expected)
                for name, payload in contents.items():
                    self.assertEqual((restored / name).read_bytes(), payload, name)

    def test_real_ninja_has_no_work_after_gnu_to_gnu_restore(self):
        self.ninja_round_trip("gnu", "gnu")

    def test_real_ninja_has_no_work_after_bsd_to_bsd_restore(self):
        self.ninja_round_trip("bsd", "bsd")

    def test_real_ninja_has_no_work_after_gnu_to_bsd_restore(self):
        self.ninja_round_trip("gnu", "bsd")

    def test_real_ninja_has_no_work_after_bsd_to_gnu_restore(self):
        self.ninja_round_trip("bsd", "gnu")

    def test_compressor_failure_never_publishes_partial_volumes(self):
        bindir = self.root / "broken-tools"
        bindir.mkdir()
        tool = bindir / "zstd"
        tool.write_text("#!/bin/sh\nprintf 'partial archive'\nexit 23\n")
        tool.chmod(0o755)
        parts, result = self.snapshot({"PATH": str(bindir) + os.pathsep + os.environ["PATH"]})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list(parts.rglob("tree.tar.zst.*")))
        self.assertFalse((parts / "stage").exists())

    def test_split_failure_never_publishes_partial_volumes(self):
        bindir = self.root / "broken-split"
        bindir.mkdir()
        # gsplit takes precedence on Linux and macOS.
        tool = bindir / "gsplit"
        tool.write_text("#!/bin/sh\ncat >/dev/null\nexit 24\n")
        tool.chmod(0o755)
        parts, result = self.snapshot({"PATH": str(bindir) + os.pathsep + os.environ["PATH"]})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list(parts.rglob("tree.tar.zst.*")))
        self.assertFalse((parts / "stage").exists())

    def test_round_trip_preserves_modes_symlinks_and_markers(self):
        # ~384 KiB of random bytes against 64 KiB volumes exercises multi-volume
        # split, round-robin wraparound across all four slots, and ordered
        # restore. Random data keeps zstd from collapsing everything into the
        # single-volume path that hid slicing bugs with compressible fixtures.
        payload = self.round_trip_payload(6)
        self.assertEqual(len(payload), 64 * 1024 * 6)
        (self.work / "src/payload.bin").write_bytes(payload)
        parts_dir, result = self.snapshot()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        volumes = sorted(
            (p for p in parts_dir.rglob("tree.tar.zst.*")
             if p.name != "tree.tar.zst."),
            key=lambda p: int(p.name.rsplit(".", 1)[1]))
        self.assertGreater(len(volumes), 4)
        self.assertLessEqual(len(volumes), 8)
        slots = {v.parent.name for v in volumes}
        self.assertEqual(slots, {"p1", "p2", "p3", "p4"})
        restored = self.root / "restored"
        restored.mkdir()
        self.restore(parts_dir, restored)
        self.assert_fixture(restored)
        self.assertEqual((restored / "src/payload.bin").read_bytes(), payload)

    def nested_round_trips(self, shell="bash", extra_env=None):
        excluded_files = (
            ".snapshot-stage-0/p1/tree.tar.zst.001",
            ".snapshot-stage-99/stage/tree.tar.zst",
            "src/.snapshot-stage-old/stale-volume",
            "download_cache/chromium.tar.xz",
            "dist/chromix-mac-arm64.zip",
            "smoke/profile/SingletonLock",
            "runtime-smoke-stage-1/report.json",
        )
        for name in excluded_files:
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("must not enter the snapshot")
        # A similar name must not be swallowed by the literal custom exclusion.
        neighbor = self.work / "handoff/custom 1xZtail/keep"
        neighbor.parent.mkdir(parents=True)
        neighbor.write_text("neighbor fixture")
        destinations = (".snapshot-stage-1", "handoff/custom parts",
                        "handoff/custom [1]*?\\tail")
        for index, relative in enumerate(destinations):
            with self.subTest(shell=str(shell), destination=relative):
                parts = self.work / relative
                stale = parts / "p4/tree.tar.zst.999"
                stale.parent.mkdir(parents=True)
                stale.write_text("stale upload")
                expected_metadata = self.tree_metadata(self.work)
                # Exercise relative ROOT/PARTS_DIR with spaces in the cwd.
                _, result = self.snapshot(
                    extra_env, parts_dir=parts.relative_to(self.root),
                    shell=shell, work=Path("work"))
                self.assertEqual(result.returncode, 0,
                                 result.stdout + result.stderr)
                self.assertNotIn("file changed as we read it", result.stderr)
                self.assertFalse(stale.exists())
                self.assertFalse((parts / "stage").exists())
                with tarfile.open(fileobj=io.BytesIO(self.snapshot_tar(parts))) as tree:
                    members = {str(Path(member.name)): member
                               for member in tree.getmembers()}
                names = set(members)
                self.assertTrue(set(self.fixture_files) <= names)
                self.assertIn("tooling/depot_tools", names)
                self.assertIn("handoff/custom 1xZtail/keep", names)
                self.assertFalse(any(
                    any(part.startswith(".snapshot-stage-") for part in Path(name).parts)
                    or name == "download_cache" or name.startswith("download_cache/")
                    or name in ("dist", "smoke") or name.startswith(("dist/", "smoke/"))
                    or name.startswith("runtime-smoke-stage-")
                    or name == relative or name.startswith(relative + "/")
                    or Path(name).name.startswith("tree.tar.zst")
                    for name in names), names)
                for name in self.fixture_files:
                    self.assertEqual(self.archive_mtime_ns(members[name]),
                                     self.mtimes_ns[name])
                for name, target in self.fixture_symlinks.items():
                    self.assertIn(name, members)
                    self.assertTrue(members[name].issym(), name)
                    self.assertEqual(members[name].linkname, target)
                restored = self.root / f"nested-restored-{index}"
                cached = restored / "download_cache/preexisting-download"
                cached.parent.mkdir(parents=True)
                cached.write_text("separately restored cache")
                for attempt in range(2):
                    with self.subTest(restore=attempt):
                        self.restore(parts, restored, extra_env)
                        self.assert_fixture(restored)
                        self.assert_tree_metadata(
                            restored, {name: expected_metadata[name] for name in names})
                        self.assertEqual(restored.stat().st_mtime_ns,
                                         self.archive_mtime_ns(members["."]))
                        self.assertEqual(cached.read_text(), "separately restored cache")
                        self.assertFalse(
                            (restored / "download_cache/chromium.tar.xz").exists())
                        self.assertFalse((restored / relative).exists())
                        self.assertFalse(list(restored.rglob(".snapshot-stage-*")))
                        self.assertFalse(list(restored.rglob("tree.tar.zst*")))
                # Subsequent cases should not package a prior custom destination.
                shutil.rmtree(parts)

    def test_nested_destinations_exclude_snapshots_and_cache_and_restore_twice(self):
        self.nested_round_trips()

    @unittest.skipUnless(BASH32.is_file(), "locally built bash 3.2 required")
    def test_nested_destinations_under_bash_3_2(self):
        self.nested_round_trips(shell=BASH32)

    @unittest.skipUnless(shutil.which("bsdtar"), "BSD tar required")
    def test_nested_destinations_under_bsd_tar(self):
        bindir = self.root / "bsd-bin"
        bindir.mkdir()
        (bindir / "tar").symlink_to(shutil.which("bsdtar"))
        self.nested_round_trips(extra_env={
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"})

    @unittest.skipUnless(shutil.which("bsdtar") and BASH32.is_file(),
                         "BSD tar and locally built bash 3.2 required")
    def test_nested_destinations_under_bsd_tar_and_bash_3_2(self):
        bindir = self.root / "bsd-bin"
        bindir.mkdir()
        (bindir / "tar").symlink_to(shutil.which("bsdtar"))
        self.nested_round_trips(shell=BASH32, extra_env={
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"})

    def test_tree_with_only_excluded_entries_restores_empty(self):
        shells = ["bash"] + ([str(BASH32)] if BASH32.is_file() else [])
        tars = ["tar"] + (["bsdtar"] if shutil.which("bsdtar") else [])
        for shell_index, shell in enumerate(shells):
            for tar_name in tars:
                with self.subTest(shell=shell, tar=tar_name):
                    base = self.root / f"empty-{shell_index}-{tar_name}"
                    work = base / "work"
                    cache = work / "download_cache/keep"
                    cache.parent.mkdir(parents=True)
                    cache.write_text("separate cache")
                    bindir = base / "bin"
                    bindir.mkdir()
                    (bindir / "tar").symlink_to(shutil.which(tar_name))
                    env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
                    parts, result = self.snapshot(
                        env, parts_dir=work / ".snapshot-stage-1",
                        work=work, shell=shell)
                    self.assertEqual(result.returncode, 0,
                                     result.stdout + result.stderr)
                    with tarfile.open(fileobj=io.BytesIO(self.snapshot_tar(parts))) as tree:
                        self.assertEqual(tree.getnames(), [".", "."])
                    restored = base / "restored"
                    restored.mkdir()
                    for attempt in range(2):
                        self.restore(parts, restored, env)
                        self.assertEqual(list(restored.iterdir()), [])
                    self.assertEqual(cache.read_text(), "separate cache")

    def test_symlinked_nested_destination_is_excluded_by_physical_path(self):
        actual = self.work / "handoff/custom parts"
        actual.mkdir(parents=True)
        alias = self.root / "parts-alias"
        alias.symlink_to(actual, target_is_directory=True)
        root_alias = self.root / "root-alias"
        root_alias.symlink_to(self.work, target_is_directory=True)
        _, result = self.snapshot(parts_dir=alias, work=root_alias)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored = self.root / "symlink-restored"
        restored.mkdir()
        self.restore(actual, restored)
        self.assert_fixture(restored)
        self.assertFalse((restored / "handoff/custom parts").exists())
        self.assertFalse(list(restored.rglob("tree.tar.zst*")))

    def test_parts_equal_to_or_containing_root_are_rejected_before_cleanup(self):
        for case in ("same", "parent", "dotdot", "same-symlink", "parent-symlink"):
            with self.subTest(case=case):
                base = self.root / case
                work = base / "work"
                (work / "src").mkdir(parents=True)
                marker = work / "src/keep"
                marker.write_text("do not delete source")
                sibling = base / "keep"
                sibling.write_text("do not delete siblings")
                parts = work if case == "same" else base
                if case == "dotdot":
                    parts = work / "src/.."
                elif case.endswith("-symlink"):
                    parts = base / "parts-link"
                    parts.symlink_to(work if case == "same-symlink" else base,
                                     target_is_directory=True)
                _, result = self.snapshot(parts_dir=parts, work=work)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must not equal or contain root", result.stderr)
                self.assertEqual(marker.read_text(), "do not delete source")
                self.assertEqual(sibling.read_text(), "do not delete siblings")
                self.assertFalse(list(base.rglob("tree.tar.zst*")))

    def test_sibling_destination_with_root_name_prefix_is_allowed(self):
        parts, result = self.snapshot(parts_dir=self.root / "work-parts")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored = self.root / "sibling-restored"
        restored.mkdir()
        self.restore(parts, restored)
        self.assert_fixture(restored)

    def test_exceeding_the_volume_budget_aborts_instead_of_uploading_broken_state(self):
        # Three 64 KiB volumes of random data against a two-volume budget must
        # fail the stage and leave no partial upload directories behind.
        (self.work / "src/payload.bin").write_bytes(self.round_trip_payload(3))
        parts_dir, result = self.snapshot(
            {"CHROMIX_SNAPSHOT_MAX_VOLUMES": "2"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("handoff budget", result.stdout + result.stderr)
        self.assertEqual(list(parts_dir.rglob("tree.tar.zst.*")), [])

    def test_more_than_four_volumes_warns_about_plan_size_caps(self):
        # Between MAX_SLOTS and MAX_VOLUMES the chain warns but keeps going:
        # re-packing hundreds of gigabytes buys nothing when the artifact
        # upload cap is the real constraint.
        (self.work / "src/payload.bin").write_bytes(self.round_trip_payload(6))
        parts_dir, result = self.snapshot()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        volumes = list(parts_dir.rglob("tree.tar.zst.*"))
        self.assertGreater(len(volumes), 4)
        self.assertLessEqual(len(volumes), 8)
        output = result.stdout + result.stderr
        self.assertIn("::warning::", output)
        self.assertIn("artifact size caps depend on the GitHub plan", output)

    def test_stage_chain_keeps_upstream_guards_in_source(self):
        stage_source = CI_STAGE.read_text(encoding="utf-8")
        # Mirror of the Windows last-stage guard: without it a chain that runs
        # out of stages would end green with no artifact and release nothing.
        self.assertIn('if [ "$STAGE_INDEX" -ge "$MAX_STAGES" ]', stage_source)
        self.assertIn(
            'die "stage $STAGE_INDEX reached max-stages $MAX_STAGES without finishing"',
            stage_source)
        # Domain substitution must never resume over an interrupted marker.
        self.assertIn('.chromix-domain-substitution-in-progress', stage_source)
        parts_source = CI_PARTS.read_text(encoding="utf-8")
        self.assertIn('CHROMIX_SNAPSHOT_VOLUME_BYTES', parts_source)
        self.assertIn('CHROMIX_SNAPSHOT_MAX_VOLUMES', parts_source)
        self.assertIn('"$SPLIT" -a 3 -d -b "$VOLUME_BYTES"', parts_source)

    def test_stage_chain_is_macos_bash_3_2_compatible(self):
        stage_source = CI_STAGE.read_text(encoding="utf-8")
        # macOS runners execute workflow steps with the system /bin/bash
        # 3.2. Three first-run failure classes came from treating this like a
        # modern bash or a GNU-only Linux toolchain:
        self.assertIn(
            'REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"',
            stage_source)
        # 1. $(( )) cannot nest quoted command substitution on bash 3.2
        #    (macos stage 1 died at remaining_min); expand to variables.
        self.assertNotIn('- "$(now_epoch)")', stage_source)
        self.assertNotIn("- \"$(now_epoch)\")", stage_source)
        self.assertIn('left=$(( (DEADLINE_EPOCH - now) / 60 ))',
                      stage_source)
        # 2. GNU timeout does not exist on macOS; coreutils ships gtimeout,
        #    so the ninja deadline and smoke checks must resolve it first.
        self.assertIn('elif command -v gtimeout >/dev/null 2>&1; then',
                      stage_source)
        self.assertNotIn(' timeout 30s ', stage_source)
        self.assertNotIn(' timeout 60s ', stage_source)

    def test_linux_build_selects_host_arch_tools_like_upstream_portablelinux(self):
        source = (REPO / "build" / "build.sh").read_text(encoding="utf-8")
        # Upstream setup_toolchain keys Node/Go to the host architecture;
        # the target only selects sysroots and GN args.
        self.assertIn('case "$HOST_ARCH" in', source)
        self.assertIn('x64) GO_ARCH=amd64', source)
        self.assertIn('arm64) GO_ARCH=arm64', source)
        self.assertNotIn('GO_ARCH="$ARCH"', source)
        self.assertIn('third_party/dawn/tools/golang/linux-$GO_ARCH/bin/go', source)
        self.assertIn('SYSROOT_ARCH=amd64', source)
        self.assertIn('SYSROOT_ARCH=arm64', source)


@unittest.skipUnless(
    Path(os.path.expanduser("~/.local/bash-3.2-for-ci/bash")).is_file(),
    "locally built bash 3.2 required (matches macOS /bin/bash)")
class PosixStageBash32ExecutionTest(unittest.TestCase):
    """Execute the handoff chain under real bash 3.2 like macOS runners do.

    Static checks cannot catch what this class caught in the first real run:
    quoted command substitution inside $(( )) dies on bash 3.2, GNU timeout
    does not exist on macOS, and a wrong REPO hop broke every $REPO path.
    Both stage paths below run with a deadline that forces the prepare
    budget under its minimum, exercising argument parsing, remaining_min,
    GITHUB_OUTPUT emission, ci-parts packing, cross-segment restore, and the
    second handoff - without compiling anything.
    """

    BASH32 = Path(os.path.expanduser("~/.local/bash-3.2-for-ci/bash"))

    def test_stage1_handoff_then_stage2_restore_and_rehandoff(self):
        import datetime
        base = Path(tempfile.mkdtemp(prefix="chromix bash32 "))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        work = base / "work"
        work.mkdir()
        out_file = base / "github_output"
        # remaining minutes minus reserve lands below the 25-minute minimum.
        deadline = int(datetime.datetime.now().timestamp()) + 40 * 60

        def run_stage(args):
            return subprocess.run(
                [str(self.BASH32), "--norc", str(CI_STAGE), *args],
                capture_output=True, text=True,
                env={**os.environ,
                     "CHROMIX_RESERVE_MINUTES": "45",
                     "GITHUB_OUTPUT": str(out_file)})

        stage1 = run_stage(["--platform", "macos", "--arch", "arm64",
                            "--workdir", str(work),
                            "--stage-index", "1", "--max-stages", "8",
                            "--deadline-epoch", str(deadline)])
        self.assertEqual(stage1.returncode, 0,
                         stage1.stdout + stage1.stderr)
        snap = work / ".snapshot-stage-1"
        volumes = list(snap.rglob("tree.tar.zst.*"))
        self.assertTrue(volumes)
        outputs = dict(line.split("=", 1)
                       for line in out_file.read_text().splitlines())
        self.assertEqual(outputs["status"], "running")
        self.assertEqual(outputs["finished"], "false")
        self.assertEqual(outputs["upload_snapshot"], "true")

        restore = base / "restore"
        shutil.copytree(snap, restore)
        out_file.write_text("")
        stage2 = run_stage(["--platform", "linux", "--arch", "x64",
                            "--workdir", str(work),
                            "--stage-index", "2", "--max-stages", "8",
                            "--from-snapshot", str(restore),
                            "--deadline-epoch", str(deadline)])
        self.assertEqual(stage2.returncode, 0,
                         stage2.stdout + stage2.stderr)
        # The consuming stage must remove the restore directory after
        # unpacking so RUNNER_TEMP never reuses stale volumes.
        self.assertFalse(restore.exists())
        self.assertTrue((work / ".snapshot-stage-2" / "p1").is_dir())


@unittest.skipUnless(shutil.which("zstd"), "zstd required")
class PosixStageRestoreRejectsMismatchedSnapshotTest(unittest.TestCase):
    def test_missing_snapshot_directory_fails_for_resume_stage(self):
        temp = tempfile.TemporaryDirectory(prefix="chromix posix reject ")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        fake_restore = root / "restore-missing"
        output_file = root / "github_output"
        with output_file.open("w") as out:
            proc = subprocess.run(
                ["bash", str(CI_STAGE), "--platform", "linux", "--arch",
                 "x64", "--workdir", str(root / "work"),
                 "--stage-index", "2", "--max-stages", "8",
                 "--from-snapshot", str(fake_restore),
                 "--deadline-epoch", str(1)],
                capture_output=True, text=True,
                env={**os.environ, "GITHUB_OUTPUT": str(output_file)})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not exist", proc.stderr + proc.stdout)


class GenPosixWorkflowTest(unittest.TestCase):
    def test_workflow_matches_generator_output(self):
        with tempfile.TemporaryDirectory(prefix="chromix workflow ") as temp:
            generated = Path(temp) / WORKFLOW.relative_to(REPO)
            generated.parent.mkdir(parents=True)
            subprocess.run([sys.executable, str(GEN_WORKFLOW)],
                           cwd=temp, check=True, capture_output=True)
            self.assertEqual(generated.read_bytes(), WORKFLOW.read_bytes())
            before = generated.read_bytes()
            subprocess.run([sys.executable, str(GEN_WORKFLOW)],
                           cwd=temp, check=True, capture_output=True)
            self.assertEqual(generated.read_bytes(), before)

    def test_workflow_has_no_collapsed_gha_expressions(self):
        # f-strings collapse ${{ ... }} to ${ ... }, which Actions cannot
        # expand; guard the generator against that regression.
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("${ inputs.", text)
        self.assertNotIn("${ runner.", text)
        self.assertNotIn("${ steps.", text)
        for token in ("${{ inputs.platform }}", "${{ inputs.arch }}",
                      "${{ inputs['max-stages'] }}", "${{ github.run_attempt }}",
                      "${{ steps.stage.outputs.finished }}",
                      "${{ inputs.artifact }}"):
            self.assertIn(token, text)

    def test_all_run_scripts_pass_bash_n_and_stage_args_are_correct(self):
        import re
        import yaml
        data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        jobs = data["jobs"]
        self.assertEqual(list(jobs), [f"posix-{i}" for i in range(1, 9)] + ["verify-linux-arm64"])
        stub = re.compile(r"\$\{\{[^}]*\}\}")
        checked = 0
        for name, job in jobs.items():
            needs = job.get("needs")
            if needs and name.startswith("posix-"):
                self.assertIn("always()", job["if"])
                self.assertIn(f"needs.{needs}.result == 'success'", job["if"])
                self.assertIn(f"needs.{needs}.outputs.finished != 'true'",
                              job["if"])
            for step in job["steps"]:
                script = step.get("run")
                if not script:
                    continue
                proc = subprocess.run(["bash", "-n", "/dev/stdin"],
                                       input=stub.sub("x", script),
                                       capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0,
                                 (name, step.get("name"), proc.stderr))
                checked += 1
        stage1 = next(s for s in jobs["posix-1"]["steps"]
                      if s.get("name") == "Run stage 1")["run"]
        stage2 = next(s for s in jobs["posix-2"]["steps"]
                      if s.get("name") == "Run stage 2")["run"]
        self.assertNotIn("--from-snapshot", stage1)
        # inputs.max_stages rendered empty because the declared key is max-stages.
        # Lock the exact input name and rendered argument shape per stage.
        self.assertIn(
            "--stage-index 1 --max-stages '${{ inputs['max-stages'] }}' "
            '--deadline-epoch "$DEADLINE_EPOCH"', stage1)
        self.assertIn("--from-snapshot \"${RUNNER_TEMP}/chromix-restore\"", stage2)
        deps = next(s for s in jobs["posix-1"]["steps"]
                    if s.get("name") == "Install Linux build dependencies")
        # Pinned Go is installed by actions/setup-go and verified in its own step;
        # this dependency step must not fetch a moving toolchain version.
        self.assertNotIn("go.dev/VERSION", deps["run"])
        setup_go = next(s for s in jobs["posix-1"]["steps"]
                        if s.get("name") == "Set up Go")
        self.assertEqual(setup_go["with"]["go-version"], "1.27.1")
        verify_go = next(s for s in jobs["posix-1"]["steps"]
                        if s.get("name") == "Verify Go version")
        self.assertIn("go version", verify_go["run"])
        self.assertIn("go1.27.1", verify_go["run"])
        last = next(s for s in jobs["posix-8"]["steps"]
                    if s.get("name") == "Run stage 8")["run"]
        self.assertIn("--stage-index 8", last)
        download = next(s for s in jobs["posix-8"]["steps"]
                        if s.get("name") == "Download tree from previous stage")
        self.assertIn("${{ inputs.artifact }}-tree-s7-attempt-${{ needs.posix-7.outputs.snapshot_attempt }}-part*",
                      download["with"]["pattern"])
        self.assertTrue(download["with"].get("merge-multiple"))
        restore = sorted(s for s in jobs["posix-3"]["steps"]
                         if s.get("name") == "Download tree from previous stage")
        self.assertEqual(len(restore), 1)
        self.assertGreaterEqual(checked, 32)

    def test_every_stage_selects_sdk_and_uploads_ready_snapshots_unless_cancelled(self):
        import yaml
        data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for index in range(1, 9):
            steps = data["jobs"][f"posix-{index}"]["steps"]
            names = [step.get("name", "") for step in steps]
            self.assertLess(names.index("Select compatible Xcode"),
                            names.index("Inspect macOS toolchain"))
            self.assertLess(names.index("Select compatible Xcode"),
                            names.index("Restore pinned source downloads"))
            snapshot_steps = [s for s in steps if s.get("name", "").startswith("Upload tree part")]
            snapshot_steps.append(next(s for s in steps if s.get("name") == "Verify handoff snapshot"))
            self.assertEqual(len(snapshot_steps), 5)
            for step in snapshot_steps:
                expected = ("!cancelled() && (steps.stage.outputs.upload_snapshot == 'true' || "
                            "steps.runtime_checkpoint.outputs.upload_snapshot == 'true')")
                if step.get("name", "").startswith("Upload tree part"):
                    expected += " && steps.checkpoint.outcome == 'success'"
                self.assertEqual(step["if"], "${{ " + expected + " }}")
                self.assertNotIn("ci-parts.sh", step.get("run", ""))
            logs = next(s for s in steps if s.get("name") == "Upload build diagnostics")
            self.assertEqual(logs["if"], "always()")

    def test_platform_workflows_reference_posix_reusable_jobs(self):
        for path in ENTRY_WORKFLOWS:
            source = path.read_text(encoding="utf-8")
            self.assertIn("./.github/workflows/build-posix-github.yml", source)
            self.assertNotIn("runs-on:", source)
            self.assertIn("secrets: inherit", source)
            self.assertEqual(source.count("artifact:"), 1)


class WorkflowInputIntegrityTest(unittest.TestCase):
    """Every inputs reference must hit a declared key of its own workflow.

    GitHub Actions resolves input names literally: `inputs.max_stages` does
    not reference the declared `max-stages` key. The first real POSIX run
    died on that mismatch (`--max-stages ''`). This audit across all workflows
    catches undeclared names, including future renames.
    """

    WORKFLOWS = REPO / ".github" / "workflows"

    def test_input_references_match_declared_input_keys(self):
        import re
        import yaml
        dotted = re.compile(r"\$\{\{\s*inputs\.([A-Za-z0-9_-]+)")
        bracketed = re.compile(r"\$\{\{\s*inputs\['([^']+)'")
        files = sorted(self.WORKFLOWS.glob("*.yml"))
        self.assertGreaterEqual(len(files), 5)
        for path in files:
            text = path.read_text(encoding="utf-8")
            data = yaml.safe_load(text)
            triggers = data.get(True) or data.get("on") or {}
            keys = set()
            for body in triggers.values():
                if isinstance(body, dict):
                    keys |= set((body.get("inputs") or {}).keys())
            refs = {m.group(1) for m in dotted.finditer(text)}
            refs |= {m.group(1) for m in bracketed.finditer(text)}
            missing = refs - keys if keys else set()
            # Workflows without declared inputs must not reference any.
            self.assertEqual(missing, set(),
                             f"{path.name}: undeclared input refs {missing}")


if __name__ == "__main__":
    unittest.main()
