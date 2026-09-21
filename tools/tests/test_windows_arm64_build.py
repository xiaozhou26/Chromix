"""Windows ARM64 cross-build guards, host toolchains and packaging regressions."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


REPO = Path(__file__).resolve().parents[2]
WINDOWS = REPO / "build/windows"
PWSH = shutil.which("pwsh") or ("/opt/pwsh/pwsh" if Path("/opt/pwsh/pwsh").is_file() else None)


def run_ps(code, **environment):
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
         '$ErrorActionPreference = "Stop"\n' + code],
        env={**os.environ, **environment}, capture_output=True, text=True, timeout=30)


class Arm64BuildSourceTest(unittest.TestCase):
    def test_entrypoints_default_to_x64_with_explicit_and_environment_overrides(self):
        for name in ("build.ps1", "ci-stage.ps1", "prepare-ungoogled.ps1", "package-win.ps1", "ci-parts.ps1"):
            with self.subTest(name=name):
                source = (WINDOWS / name).read_text()
                self.assertIn('[ValidateSet("x64", "arm64")]', source)
                self.assertIn('$env:CHROMIX_TARGET_ARCH', source)
                self.assertIn('else { "x64" }', source)

    def test_target_overlay_is_last_without_changing_host_tools(self):
        for name in ("build.ps1", "ci-stage.ps1"):
            source = (WINDOWS / name).read_text()
            merge = source[source.index('$mergeArgs ='):source.index('python @mergeArgs')]
            self.assertLess(merge.index('build\\args.windows.gn'), merge.index('build\\args.windows.arm64.gn'))
            self.assertIn('if ($Arch -eq "arm64")', merge)
            self.assertIn('third_party\\ninja\\ninja.exe', source)
            self.assertIn('third_party\\node\\win', source)
            self.assertIn('python tools\\rust\\build_bindgen.py --skip-test', source)
            self.assertNotRegex(source, r'host_cpu\s*=\s*"arm64"|Replace\("x64", "arm64"\)')
        stage = (WINDOWS / "ci-stage.ps1").read_text()
        self.assertIn('-arch=x64 -host_arch=x64', stage)
        self.assertIn('--arch $Arch --workdir $WorkDir --cache-dir $UpstreamCacheDir', stage)

    def test_real_gn_merge_preserves_x64_default_and_arm64_final_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            upstream = root / "upstream.gn"
            upstream.write_text('target_cpu="x64"\nhost_cpu="x64"\nis_debug=true\n')
            for arch in ("x64", "arm64"):
                for profile in (None, "fast", "release"):
                    with self.subTest(arch=arch, profile=profile):
                        output = root / "args.gn"
                        command = [sys.executable, str(REPO / "tools/merge_gn_args.py")]
                        if profile:
                            command.extend(["--build-profile", profile])
                        command.extend([str(output), str(upstream), str(REPO / "build/args.windows.gn")])
                        if arch == "arm64":
                            command.append(str(REPO / "build/args.windows.arm64.gn"))
                        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        args = output.read_text()
                        self.assertEqual(re.findall(r'^target_cpu\s*=\s*"([^"\n]+)"', args, re.M), [arch])
                        self.assertIn('host_cpu="x64"', args)
                        self.assertIn('is_debug = false', args)

    @unittest.skipUnless(PWSH, "PowerShell is unavailable")
    def test_both_entrypoint_merges_preserve_pgo_only_for_restored_arm64(self):
        with tempfile.TemporaryDirectory(prefix='windows PGO merge ') as temp:
            root = Path(temp)
            core, windows, out = (root / name for name in ('core', 'windows', 'out'))
            for path in (core, windows, out):
                path.mkdir()
            (core / 'flags.gn').write_text('chrome_pgo_phase=0\nsymbol_level=2\n')
            (windows / 'flags.windows.gn').write_text('chrome_pgo_phase=2\n')
            for name, variable in (('build.ps1', '$mergedArgs'), ('ci-stage.ps1', '$gnArgs')):
                source = (WINDOWS / name).read_text()
                start = source.index(variable + ' = Join-Path')
                end = source.index('if ($Arch -eq "arm64") {\n  & "$PSScriptRoot\\assert-target-arch.ps1"', start)
                merge = source[start:end]
                for arch in ('arm64', 'x64'):
                    for restored in (False, True):
                        for profile in ('native', 'fast', 'release'):
                            with self.subTest(name=name, arch=arch, restored=restored, profile=profile):
                                (out / 'args.gn').write_text('chrome_pgo_phase = 2 # donor\ntarget_cpu="arm64"\n')
                                receipt = root / '.chromix-upstream-restored.json'
                                receipt.unlink(missing_ok=True)
                                if restored:
                                    receipt.write_text('{}')
                                result = run_ps(r'''
$Repo = $env:TEST_REPO
$Src = $env:TEST_ROOT
$Out = $OutDir = Join-Path $Src "out"
$UngoogledTooling = Join-Path $Src "core"
$WindowsTooling = Join-Path $Src "windows"
$Arch = $env:TEST_ARCH
$RestoredUpstream = $env:TEST_RESTORED -eq "1"
$BuildProfile = $env:TEST_PROFILE
function python {
  & $env:TEST_PYTHON @args
  $global:LASTEXITCODE = $LASTEXITCODE
}
''' + merge, TEST_REPO=str(REPO), TEST_ROOT=str(root), TEST_ARCH=arch,
                                    TEST_RESTORED=str(int(restored)), TEST_PROFILE=profile, TEST_PYTHON=sys.executable)
                                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                                text = (out / 'args.gn').read_text()
                                expected = '2 # donor' if arch == 'arm64' and restored else '0'
                                self.assertIn('chrome_pgo_phase = ' + expected, text)
                                self.assertEqual(text.count('chrome_pgo_phase'), 1)
                                self.assertIn(f'target_cpu = "{arch}"', text)

    def test_snapshot_guard_precedes_source_migration_and_tool_downloads(self):
        stage = (WINDOWS / "ci-stage.ps1").read_text()
        restore = stage.index('& $sevenZip x "C:\\restore\\tree.7z.001"')
        guard = stage.index('assert-target-arch.ps1', restore)
        self.assertLess(guard, stage.index('$domainProgress ='))
        self.assertLess(guard, stage.index('tools\\restore_upstream_cache.py'))
        self.assertLess(guard, stage.index('& "$PSScriptRoot\\prepare-ungoogled.ps1"'))
        self.assertIn('-RequireMarker:($FromArtifact -and $Arch -eq "arm64")', stage)
        prepare = (WINDOWS / "prepare-ungoogled.ps1").read_text()
        self.assertLess(prepare.index('assert-target-arch.ps1'), prepare.index('$Python ='))
        self.assertIn('Assert-Arm64RustToolchain\n  if (-not $RestoredUpstream)', prepare)
        self.assertIn('"--verify-only"', prepare)
        self.assertIn('assert-target-arch.ps1', (WINDOWS / "ci-parts.ps1").read_text())

    def test_arm64_requires_neutral_persona_hooks_not_x86_gpu_templates(self):
        package = (WINDOWS / "package-win.ps1").read_text()
        self.assertIn('$requiredMarkers = @("uxr-webgl-vendor", "uxr-webgl-renderer")', package)
        gate = package.index('if ($Arch -eq "x64") { $requiredMarkers += @(')
        end = package.index(') }', gate)
        for marker in ('Google Inc. (Intel)', 'Google Inc. (NVIDIA)', 'Google Inc. (AMD)'):
            self.assertIn(marker, package[gate:end])
        self.assertIn('tools\\verify_windows_bundle.py") --bundle $Bundle --arch $Arch', package)
        self.assertLess(package.index('verify_windows_bundle.py'), package.index('Compress-Archive'))
        self.assertNotIn('--runtime', package)
        self.assertIn('"chromix-win-arm64.zip"', package)
        self.assertIn('"$hash  $assetName"', package)

    def test_vs_checks_host_tools_arm64_libs_sdk_and_redist_without_install_mutation(self):
        source = (WINDOWS / "assert-arm64-toolchain.ps1").read_text()
        for marker in ("Microsoft.VisualStudio.Component.VC.Tools.ARM64", "[17.0,18.0)",
                       "bin\\Hostx64\\x64\\cl.exe", "bin\\Hostx64\\arm64\\cl.exe",
                       "lib\\arm64\\libcmt.lib", "lib\\arm64\\msvcrt.lib",
                       'ensure-windows-sdk.ps1" -Arch arm64 -ChromiumVersion $ChromiumVersion',
                       "arm64\\Microsoft.VC143.CRT", "$env:GYP_MSVS_OVERRIDE_PATH"):
            self.assertIn(marker, source)
        self.assertNotIn('Start-Process', source)
        self.assertNotIn('Remove-Item', source)
        self.assertIn('msvcp140_atomic_wait.dll', (WINDOWS / "package-win.ps1").read_text())

    @unittest.skipUnless(PWSH, "PowerShell is unavailable")
    def test_all_windows_scripts_parse(self):
        result = run_ps(r'''
Get-ChildItem -LiteralPath $env:WINDOWS_SCRIPTS -Filter *.ps1 | ForEach-Object {
  $tokens = $null
  $errors = $null
  [Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$tokens, [ref]$errors) | Out-Null
  if ($errors.Count) { throw ($errors | Out-String) }
}
''', WINDOWS_SCRIPTS=str(WINDOWS))
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(PWSH, "PowerShell is unavailable")
class Arm64SnapshotTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="windows arch ")
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name) / "work"
        self.work.mkdir()

    def put(self, name, value):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def guard(self, arch="arm64", *, require=False):
        return run_ps(r'''
& $env:GUARD -WorkDir $env:WORK -Arch $env:ARCH -Initialize -RequireMarker:($env:REQUIRE -eq "1")
''', GUARD=str(WINDOWS / "assert-target-arch.ps1"), WORK=str(self.work), ARCH=arch, REQUIRE=str(int(require)))

    def test_cold_and_partial_same_arch_snapshots_resume(self):
        cold = self.guard()
        self.assertEqual(cold.returncode, 0, cold.stderr)
        self.assertEqual((self.work / '.chromix-target-arch').read_text().strip(), 'arm64')
        self.put('src/.chromix-layer-in-progress', 'chromix')
        partial = self.guard(require=True)
        self.assertEqual(partial.returncode, 0, partial.stderr)
        self.put('src/out/Chromix/args.gn', 'target_cpu = "arm64"\nhost_cpu = "x64"\n')
        resumed = self.guard(require=True)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)

    def test_wrong_missing_or_forged_arch_never_relabels_existing_source(self):
        self.put('src/.chromix-source-ready', 'old ready key')
        result = self.guard()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no target architecture marker', result.stderr)
        self.assertFalse((self.work / '.chromix-target-arch').exists())
        self.put('.chromix-target-arch', 'x64')
        result = self.guard()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('marker mismatch', result.stderr)
        self.assertEqual((self.work / '.chromix-target-arch').read_text(), 'x64')
        self.put('.chromix-target-arch', 'arm64')
        result = self.guard('x64')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('marker mismatch', result.stderr)

    def test_artifact_without_source_or_marker_still_fails_closed(self):
        result = self.guard(require=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.work / '.chromix-target-arch').exists())

    def test_marker_does_not_override_args_or_upstream_receipt(self):
        self.put('.chromix-target-arch', 'arm64')
        for content in ('target_cpu="x64"\n', 'target_cpu="arm64"\ntarget_cpu="x64"\n',
                        'target_cpu="arm64"\ntarget_cpu="arm64"\n', 'target_cpu=getenv("ARCH")\n',
                        'is_debug=false\n', 'target_cpu="arm64" + "x"\n'):
            with self.subTest(args=content):
                self.put('src/out/Chromix/args.gn', content)
                failed = self.guard()
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn('target_cpu mismatch', failed.stderr)
        self.put('src/out/Chromix/args.gn', 'target_cpu="arm64"\n')
        self.put('src/.chromix-upstream-restored.json', '{"arch":"x64"}')
        failed = self.guard()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('receipt platform/architecture mismatch', failed.stderr)

    def test_existing_restore_requires_receipt_identity_before_initializing_marker(self):
        self.put('src/out/Default/args.gn', 'target_cpu="arm64"\nhost_cpu="x64"\n')
        good = {'platform': 'windows', 'arch': 'arm64',
                'identity': {'platform': 'windows', 'arch': 'arm64'}}
        for receipt in (None, [], [good, good], {}, {'arch': 'arm64'}, {**good, 'arch': 'x64'},
                        {**good, 'platform': 'linux'}, {**good, 'identity': {'arch': 'x64'}}):
            with self.subTest(receipt=receipt):
                self.put('src/.chromix-upstream-restored.json', json.dumps(receipt))
                failed = self.guard()
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn('receipt platform/architecture mismatch', failed.stderr)
                self.assertFalse((self.work / '.chromix-target-arch').exists())
        self.put('src/.chromix-upstream-restored.json', json.dumps(good))
        code = r'''
function python {
  if ($args[1] -ne "--phase" -or $args[2] -ne "verify" -or
      $args[4] -ne "windows" -or $args[6] -ne "arm64") { throw "wrong restore verification interface" }
  if (Test-Path (Join-Path $env:WORK ".chromix-target-arch")) { throw "marker initialized before receipt verification" }
  $global:LASTEXITCODE = [int]$env:VERIFY_RC
}
& $env:GUARD -WorkDir $env:WORK -Arch arm64 -Initialize
'''
        for rc in ('1', '0'):
            result = run_ps(code, GUARD=str(WINDOWS / 'assert-target-arch.ps1'),
                            WORK=str(self.work), VERIFY_RC=rc)
            self.assertEqual(result.returncode == 0, rc == '0', result.stderr)
            self.assertEqual((self.work / '.chromix-target-arch').exists(), rc == '0')
        (self.work / '.chromix-target-arch').unlink()
        failed = self.guard(require=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('no target architecture marker', failed.stderr)
        self.assertFalse((self.work / '.chromix-target-arch').exists())

    def test_all_known_output_directories_are_checked_and_unknown_output_rejected(self):
        self.put('.chromix-target-arch', 'arm64')
        self.put('src/out/Chromix/args.gn', 'target_cpu="arm64"\n')
        args = self.put('src/out/Default/args.gn', 'target_cpu="x64"\n')
        self.assertNotEqual(self.guard().returncode, 0)
        args.unlink()
        self.put('src/out/Default/obj/object.obj', 'x64 object')
        failed = self.guard()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('no architecture-bearing args.gn', failed.stderr)

    def test_legacy_x64_snapshot_remains_compatible(self):
        self.put('src/.chromix-source-ready', 'legacy unchanged')
        self.put('src/.chromix-upstream-restored.json', json.dumps({
            'platform': 'windows', 'arch': 'x64', 'identity': {'platform': 'windows', 'arch': 'x64'}}))
        self.put('src/out/Default/args.gn', 'target_cpu="x64"\ntarget_cpu = "x64"\n')
        passed = self.guard('x64')
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertEqual((self.work / 'src/.chromix-source-ready').read_text(), 'legacy unchanged')

    def test_default_parameters_validate_environment_and_explicit_override(self):
        for name in ('build.ps1', 'ci-stage.ps1', 'prepare-ungoogled.ps1', 'package-win.ps1', 'ci-parts.ps1'):
            source = (WINDOWS / name).read_text()
            start = source.index('param(')
            validation = next(line for line in source.splitlines() if line.startswith('if ($Arch -cnotin'))
            params = source[start:source.index('$ErrorActionPreference', start)] + validation + '\n'
            for environment, explicit, expected in (('', '', 'x64'), ('arm64', '', 'arm64'),
                                                    ('arm64', 'x64', 'x64'), ('bad', '', None)):
                with self.subTest(name=name, environment=environment, explicit=explicit):
                    code = 'function Test-Parameters {\n' + params + '\nWrite-Output $Arch\n}\n'
                    code += r'''
$options = @{}
if ($env:EXPLICIT) { $options.Arch = $env:EXPLICIT }
'''
                    mandatory = {'prepare-ungoogled.ps1': '-Root fixture -Repo fixture',
                                 'package-win.ps1': '-Out fixture -Dest fixture',
                                 'ci-parts.ps1': '-Root fixture -PartsDir fixture'}.get(name, '')
                    result = run_ps(code + 'Test-Parameters ' + mandatory + ' @options',
                                    CHROMIX_TARGET_ARCH=environment, EXPLICIT=explicit)
                    if expected is None:
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.strip(), expected)

    def test_upstream_opt_in_is_target_independent(self):
        source = (WINDOWS / 'ci-stage.ps1').read_text()
        policy = source[source.index('$RequireUpstreamCache ='):source.index('$PartsDir =')]
        for arch in ('x64', 'arm64'):
            for option in ('none', 'switch', 'run', 'env'):
                with self.subTest(arch=arch, option=option):
                    result = run_ps(r'''
$Arch = $env:TEST_ARCH
$UseUpstreamCache = $env:OPTION -eq "switch"
$UpstreamRunId = if ($env:OPTION -eq "run") { "123" } else { "" }
''' + policy + r'''
@{ required = [bool]$RequireUpstreamCache; prefer = $env:CHROMIX_PREFER_UPSTREAM_CACHE } | ConvertTo-Json -Compress
''', TEST_ARCH=arch, OPTION=option, CHROMIX_USE_UPSTREAM_CACHE='1' if option == 'env' else '0',
                                    CHROMIX_PREFER_UPSTREAM_CACHE='1')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    data = json.loads(result.stdout)
                    self.assertEqual(data['prefer'], '1')
                    self.assertEqual(data['required'], option != 'none')


class Arm64RustToolchainTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('windows_rust_merge', WINDOWS / 'prep_rust_toolchain.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        temp = tempfile.TemporaryDirectory(prefix='windows rust arm64 ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.destination = self.root / 'rust-toolchain'
        self.sources = []
        for suffix, triple in (('x64', 'x86_64-pc-windows-msvc'), ('arm', 'aarch64-pc-windows-msvc')):
            source = self.root / f'rust-toolchain-{suffix}'
            self.sources.append(source)
            for name in ('cargo.exe', 'rustc.exe'):
                self.put(source / 'rustc/bin' / name, suffix)
            for name in ('std', 'core', 'alloc', 'compiler_builtins'):
                self.put(source / f'rust-std-{triple}/lib/rustlib/{triple}/lib/lib{name}-fixture.rlib', triple)

    @staticmethod
    def put(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def test_arm64_standard_library_merges_without_replacing_host_executables(self):
        self.module.merge_toolchain(self.sources, self.destination)
        self.module.verify(self.destination, self.root, 'arm64')
        for name in ('cargo.exe', 'rustc.exe'):
            self.assertEqual((self.destination / 'bin' / name).read_text(), 'x64')
        for triple in ('x86_64-pc-windows-msvc', 'aarch64-pc-windows-msvc'):
            self.assertEqual((self.destination / f'lib/rustlib/{triple}/lib/libstd-fixture.rlib').read_text(), triple)

    def test_missing_or_empty_target_std_fails_before_version_stamp(self):
        self.module.merge_toolchain(self.sources, self.destination)
        path = self.destination / 'lib/rustlib/aarch64-pc-windows-msvc/lib/libcore-fixture.rlib'
        for content in ('', None):
            if content is None:
                path.unlink()
            else:
                path.write_text(content)
            with self.subTest(content=content), self.assertRaises(SystemExit), mock.patch('sys.stderr'):
                self.module.verify(self.destination, self.root, 'arm64')
            self.assertFalse((self.destination / 'INSTALLED_VERSION').exists())

    def test_verify_only_preserves_bindgen_and_existing_toolchain(self):
        self.module.merge_toolchain(self.sources, self.destination)
        bindgen = self.destination / 'bin/bindgen.exe'
        bindgen.write_text('x64 bindgen retained')
        result = subprocess.run([sys.executable, str(WINDOWS / 'prep_rust_toolchain.py'),
                                 '--third-party-root', str(self.root), '--arch', 'arm64', '--verify-only'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(bindgen.read_text(), 'x64 bindgen retained')


@unittest.skipUnless(PWSH, "PowerShell is unavailable")
class Arm64VisualStudioTest(unittest.TestCase):
    def test_preflight_checks_real_fixture_paths_and_keeps_host_target_separate(self):
        with tempfile.TemporaryDirectory(prefix='windows vs arm64 ') as temp:
            root = Path(temp)
            installation = root / 'VS2022'
            programs = root / 'Program Files (x86)'
            sdk = programs / 'Windows Kits/10'
            vc = installation / 'VC/Tools/MSVC/14.44.35207'
            helper = WINDOWS / 'assert-arm64-toolchain.ps1'
            source = helper.read_text()
            version = installation / 'VC/Auxiliary/Build/Microsoft.VCToolsVersion.default.txt'
            version.parent.mkdir(parents=True)
            version.write_text('14.44.35207')
            from tools.tests.test_windows_sdk import SDK_VERSION, sdk_fixture
            sdk_fixture(sdk)
            for base, relative in re.findall(r'\(Join-Path \$(vc|sdk) "([^"]+)"\)', source):
                path = (vc if base == 'vc' else sdk) / relative.replace('\\', '/')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('nonempty fixture')
            redist = installation / 'VC/Redist/MSVC/14.44.35207/arm64/Microsoft.VC143.CRT'
            redist.mkdir(parents=True)
            for name in ('msvcp140.dll', 'msvcp140_atomic_wait.dll', 'vccorlib140.dll', 'vcruntime140.dll'):
                (redist / name).write_text('ARM64 runtime')
            code = r'''
${env:ProgramFiles(x86)} = $env:PROGRAMS
& $env:CHECK -Installation $env:INSTALLATION
Write-Output "SELECTED=$env:GYP_MSVS_OVERRIDE_PATH"
'''
            environment = dict(PROGRAMS=str(programs), CHECK=str(helper), INSTALLATION=str(installation),
                               WINDOWSSDKDIR='')
            passed = run_ps(code, **environment)
            self.assertEqual(passed.returncode, 0, passed.stderr)
            self.assertIn(f'SELECTED={installation}', passed.stdout)
            for path in (vc / 'bin/Hostx64/arm64/cl.exe', sdk / f'Lib/{SDK_VERSION}/ucrt/arm64/ucrt.lib',
                         sdk / 'Debuggers/arm64/dbghelp.dll', redist / 'vcruntime140.dll'):
                with self.subTest(missing=path):
                    saved = path.read_bytes()
                    path.unlink()
                    failed = run_ps(code, **environment)
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertNotIn('SELECTED=', failed.stdout)
                    path.write_bytes(saved)


@unittest.skipUnless(PWSH, "PowerShell is unavailable")
class Arm64PackageTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='windows package arm64 ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.out = self.root / 'src/out/Chromix'
        self.out.mkdir(parents=True)
        (self.root / 'src/LICENSE').write_text('Chromium fixture license')
        self.dest = self.root / 'dist'
        self.dest.mkdir()
        for name in ('chrome.exe', 'chrome.dll', 'chrome_elf.dll', 'libEGL.dll', 'libGLESv2.dll',
                     'msvcp140.dll', 'vcruntime140.dll', 'msvcp140_atomic_wait.dll', 'vccorlib140.dll'):
            (self.out / name).write_bytes(self.pe())
        for name in ('chrome_100_percent.pak', 'chrome_200_percent.pak', 'resources.pak', 'icudtl.dat',
                     'v8_context_snapshot.bin', 'chrome.exe.manifest'):
            (self.out / name).write_text('fixture')
        (self.out / 'locales').mkdir()
        (self.out / 'locales/en-US.pak').write_text('locale')

    @staticmethod
    def pe(machine=0xAA64, markers=True):
        data = bytearray(64 + 24 + 112 + 40)
        data[:2] = b'MZ'
        struct.pack_into('<I', data, 60, 64)
        data[64:68] = b'PE\0\0'
        struct.pack_into('<HH', data, 68, machine, 1)
        struct.pack_into('<HH', data, 84, 112, 2)
        struct.pack_into('<H', data, 88, 0x20B)
        if markers:
            data.extend(b'uxr-webgl-vendor\0uxr-webgl-renderer\0')
        return data

    def package(self, arch='arm64'):
        return run_ps(r'''
function python {
  & $env:TEST_PYTHON @args
  $global:LASTEXITCODE = $LASTEXITCODE
}
& $env:PACKAGE -Out $env:OUT -Dest $env:DEST -Arch $env:ARCH
''', TEST_PYTHON=sys.executable, PACKAGE=str(WINDOWS / 'package-win.ps1'),
                      OUT=str(self.out), DEST=str(self.dest), ARCH=arch)

    def test_real_package_and_verifier_accept_arm64_without_intel_gpu_strings(self):
        if not (REPO / 'tools/verify_windows_bundle.py').is_file():
            self.skipTest('main-agent bundle verifier is not available yet')
        result = self.package()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        asset = self.dest / 'chromix-win-arm64.zip'
        self.assertTrue(asset.is_file())
        self.assertFalse((self.dest / 'chromix-win-x64.zip').exists())
        self.assertIn('  chromix-win-arm64.zip', (self.dest / 'SHA256SUMS').read_text())
        with zipfile.ZipFile(asset) as archive:
            for name in ('chrome.exe', 'msvcp140_atomic_wait.dll', 'vccorlib140.dll', 'chromix.cmd'):
                self.assertIn('chromix/' + name, archive.namelist())

    def test_foreign_runtime_pe_prevents_zip_creation(self):
        if not (REPO / 'tools/verify_windows_bundle.py').is_file():
            self.skipTest('main-agent bundle verifier is not available yet')
        (self.out / 'vcruntime140.dll').write_bytes(self.pe(0x8664))
        result = self.package()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('wrong PE architecture', result.stdout + result.stderr)
        self.assertFalse((self.dest / 'chromix-win-arm64.zip').exists())

    def test_missing_required_hook_or_arm64_runtime_fails_packaging(self):
        (self.out / 'chrome.dll').write_bytes(self.pe(markers=False))
        result = self.package()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing required WebGL persona marker', result.stderr)
        (self.out / 'chrome.dll').write_bytes(self.pe())
        (self.out / 'vccorlib140.dll').unlink()
        result = self.package()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ARM64 VC143 runtime is missing', result.stderr)
        self.assertFalse((self.dest / 'chromix-win-arm64.zip').exists())

    def test_x64_still_requires_desktop_gpu_personas(self):
        result = self.package('x64')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Google Inc. (Intel)', result.stderr)


@unittest.skipUnless(PWSH, "PowerShell is unavailable")
class Arm64FinalBundleTest(unittest.TestCase):
    def test_cross_bundle_metadata_never_executes_arm_browser_or_fingerprint_audit(self):
        from tools.tests.test_windows_final_bundle import WindowsFinalBundleTest
        fixture = WindowsFinalBundleTest()
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        arm_asset = fixture.dist / 'chromix-win-arm64.zip'
        fixture.asset.rename(arm_asset)
        fixture.manifest.write_text(f'{fixture.digest}  chromix-win-arm64.zip\n')
        code = fixture.script.read_text()
        code = code.replace('$Root = $env:TEST_ROOT', '$Root = $env:TEST_ROOT\n$Arch = "arm64"\n$Repo = $Root')
        code = code.replace('function Invoke-BoundedBrowser {', '''function python {
  if ($args[1] -ne "--bundle" -or $args[3] -ne "--arch" -or $args[4] -ne "arm64") { throw "wrong verifier interface" }
  Write-Host "MOCK_PE_METADATA"
  $global:LASTEXITCODE = if ($env:TEST_BAD_METADATA -eq "1") { 7 } else { 0 }
}
function Write-OutVar($key, $value) { Write-Host "$key=$value" }
function Invoke-BoundedBrowser {''')
        fixture.script.write_text(code)
        good = fixture.verify()
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertIn('MOCK_PE_METADATA', good.stdout)
        self.assertIn('runtime_verified=false', good.stdout)
        self.assertIn('status=compiled', good.stdout)
        self.assertNotIn('MOCK_DOM', good.stdout)
        self.assertNotIn('MOCK_FINGERPRINT', good.stdout)
        bad = fixture.verify(TEST_BAD_METADATA='1')
        self.assertNotEqual(bad.returncode, 0)
        self.assertNotIn('status=compiled', bad.stdout)
        self.assertNotIn('MOCK_DOM', bad.stdout)


if __name__ == '__main__':
    unittest.main()
