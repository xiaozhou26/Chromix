import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools.platform_pins import load_pins


REPO = Path(__file__).resolve().parents[2]
STAGE = REPO / "build/windows/ci-stage.ps1"
WORKFLOW = REPO / ".github/workflows/build-win-x64-github.yml"
PREPARE = REPO / "build/windows/prepare-ungoogled.ps1"


def windows_source_fixture(repo):
    from tools.tests.test_fetch_upstream_cache import synthetic_windows_source

    source = synthetic_windows_source(repo)
    source['artifacts']['arm64'] = dict(source['artifacts']['x64'], id=104, name='build-artifact-arm',
                                      run_id=103, workflow_path='.github/workflows/build-arm.yml')
    return source


def stage_budget_source() -> str:
    stage = STAGE.read_text()
    return stage[stage.index('$StageMinutes ='):stage.index('\nfunction Write-OutVar')]


def workflow_job(source: str, name: str) -> str:
    match = re.search(rf"(?ms)^  {re.escape(name)}:\n.*?(?=^  [\w-]+:|\Z)", source)
    if match is None:
        raise AssertionError(f"Missing workflow job: {name}")
    return match.group(0)


def workflow_runs(source: str) -> list[str]:
    runs = re.findall(r"(?m)^        run: ([^\n]*(?:\n          [^\n]*)*)", source)
    return [textwrap.dedent(run[2:]) if run.startswith("|\n") else run for run in runs]


class WindowsUpstreamCacheRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stage = STAGE.read_text(encoding="utf-8")
        cls.prepare = PREPARE.read_text(encoding="utf-8")
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.build_one = workflow_job(cls.workflow, "build-1")
        start = cls.stage.index("if ($StageIndex -eq 1 -and -not $FromArtifact")
        end = cls.stage.index('\nif (-not (Test-Path (Join-Path $Src ".chromix-source-ready"))', start)
        cls.cache = cls.stage[start:end]
        start = cls.prepare.index('if ($RestoredUpstream) {\n  Invoke-Checked $Python @(\n'
                                  '    (Join-Path $Repo "tools\\prepare_restored_build.py")')
        end = cls.prepare.index('\nif (-not (Test-Marker ".chromix-source-unpacked"', start)
        cls.restored_prep = cls.prepare[start:end]

    def test_validation_runs_inline_before_compilation(self):
        self.assertIn("if: ${{ inputs.resume_run_id == '' }}", self.build_one)
        self.assertNotIn("  validate:", self.workflow)
        self.assertNotIn("needs: validate", self.build_one)
        self.assertIn("UpstreamRunId = $env:UPSTREAM_RUN_ID", self.build_one)
        self.assertIn("if ($ValidateOnly -or ($StageIndex -eq 1 -and -not $FromArtifact)) {", self.stage)
        self.assertLess(self.stage.index('V8 Torque validation failed'), self.stage.index('$ninjaBudget ='))

    def test_fetch_and_restore_only_fresh_opted_in_stage_one_before_preparation(self):
        self.assertRegex(self.cache, r"^if \(\$StageIndex -eq 1 -and -not \$FromArtifact -and\s+"
                         r'-not \(Test-Path \$Src\) -and\s+\(\$RequireUpstreamCache -or '
                         r'\$env:CHROMIX_PREFER_UPSTREAM_CACHE -eq "1"\)\) \{')
        self.assertNotIn("$ValidateOnly", self.cache)
        self.assertEqual(self.stage.count("fetch_upstream_cache.py"), 1)
        self.assertEqual(self.stage.count("--phase restore"), 1)
        self.assertLess(self.stage.index("\nFree-Disk\n"), self.stage.index(self.cache))
        self.assertLess(self.stage.index("--phase restore"), self.stage.index('& "$PSScriptRoot\\prepare-ungoogled.ps1"'))
        self.assertIn('"--platform", "windows", "--arch", $Arch, "--destination", $UpstreamCacheDir', self.cache)
        self.assertEqual(self.stage.count('"--arch", $Arch'), 1)
        for path in (STAGE, PREPARE, REPO / 'build/windows/build.ps1'):
            self.assertNotRegex(path.read_text(), r'--arch(?: x64|", "x64")')
        self.assertIn('-arch=x64 -host_arch=x64', self.stage)
        self.assertIn('--arch $Arch --workdir $WorkDir --cache-dir $UpstreamCacheDir', self.cache)
        for number in range(2, 13):
            job = workflow_job(self.workflow, f"build-{number}")
            self.assertIn(f"-StageIndex {number} -MaxStages 12 -FromArtifact", job)
            import yaml
            steps = yaml.safe_load(job)[f"build-{number}"]["steps"]
            stage = next(step for step in steps if step.get("id") == "stage")
            self.assertNotIn("UPSTREAM_RUN_ID", str(stage))
            self.assertNotIn("fetch_upstream_cache.py", job)
            self.assertNotIn("-UseUpstreamCache", job)

    def test_every_build_stage_always_uploads_only_small_reuse_evidence(self):
        import yaml

        jobs = yaml.safe_load(self.workflow)["jobs"]
        evidence_paths = {r"C:\c\chromix\upstream-reuse\baseline.json",
                          r"C:\c\chromix\upstream-reuse\result.json",
                          r"C:\c\chromix\upstream-cache-preparation.json"}
        for number in range(1, 13):
            with self.subTest(stage=number):
                steps = jobs[f"build-{number}"]["steps"]
                uploads = [step for step in steps if step.get("uses") == "actions/upload-artifact@v4"
                           and "upstream-reuse" in step["with"]["path"]]
                self.assertEqual(len(uploads), 1)
                upload = uploads[0]
                self.assertEqual(upload["if"], "${{ always() }}")
                self.assertEqual(upload["with"]["if-no-files-found"], "ignore")
                self.assertIn("${{ github.run_attempt }}", upload["with"]["name"])
                paths = set(upload["with"]["path"].splitlines())
                self.assertTrue(evidence_paths <= paths)
                if number > 1:
                    self.assertEqual(paths, evidence_paths)
                    self.assertIn("${{ github.job }}", upload["with"]["name"])
                self.assertFalse(any("*" in path or "obj" in path or "parts" in path for path in paths))
                self.assertLess(steps.index(upload), next(index for index, step in enumerate(steps)
                                                         if step.get("name") == "Ensure build tree snapshot"))

    def test_snapshot_outputs_fail_closed_but_keep_small_diagnostics(self):
        import yaml

        self.assertIn('Write-OutVar snapshot_safe $(if ($env:CHROMIX_WINDOWS_VERIFY_SOURCE_REPO -or '
                      '$env:CHROMIX_WINDOWS_VERIFY_SOURCE_SHA) { "false" } else { "true" })\nAssert-CiScripts', self.stage)
        tracked_start = self.stage.index("function Invoke-Tracked {")
        tracked_end = self.stage.index("function Get-FreeGB", tracked_start)
        tracked = self.stage[tracked_start:tracked_end]
        self.assertLess(tracked.index("Write-OutVar snapshot_safe false"),
                        tracked.index("$tracked = Start-TrackedProcess"))
        self.assertNotIn("snapshot_safe true", tracked[tracked.rindex("  } catch {"):])
        jobs = yaml.safe_load(self.workflow)["jobs"]
        for number in range(1, 13):
            steps = jobs[f"build-{number}"]["steps"]
            for step in steps:
                name = step.get("name", "")
                if name == "Ensure build tree snapshot" or name.startswith("Upload tree part "):
                    self.assertIn("steps.stage.outputs.snapshot_safe == 'true'", step["if"])
                    self.assertNotIn("snapshot_safe != 'false'", step["if"])
                elif name in ("Upload upstream cache diagnostics", "Upload restored reuse evidence"):
                    self.assertEqual(step["if"], "${{ always() }}")

    def test_reuse_hooks_wrap_only_actual_chrome_and_precede_packaging_or_throw(self):
        before = self.stage.index('restored_reuse_evidence.py") --phase before')
        built = self.stage.index('$rc = Invoke-Tracked -File $Ninja')
        after = self.stage.index('restored_reuse_evidence.py") --phase after')
        self.assertLess(self.stage.index('& $Ninja -C $OutDir -n chrome'), before)
        self.assertLess(self.stage.rindex('if ($ValidateOnly -or ($StageIndex -eq 1 -and -not $FromArtifact)) {'), before)
        self.assertLess(self.stage.index('if ($ninjaBudget -lt 20)'), before)
        self.assertLess(before, built)
        self.assertLess(built, after)
        self.assertLess(after, self.stage.index('if ($rc -eq 0) {', after))
        self.assertLess(after, self.stage.index('if ($rc -eq 124) {', after))
        self.assertIn('--ninja $Ninja --target chrome --exit-code $rc', self.stage)
        self.assertEqual(self.stage.count('restored_reuse_evidence.py'), 2)

    def test_resume_verifies_receipt_and_ready_key_before_read_only_source_check(self):
        restore = self.stage.index('& $sevenZip x "C:\\restore\\tree.7z.001"')
        verify = self.stage.index("--phase verify", restore)
        prepare = self.stage.index('& "$PSScriptRoot\\prepare-ungoogled.ps1"', verify)
        source_check = self.stage.index('tools\\verify_patch_stack.py', prepare)
        self.assertLess(verify, prepare)
        self.assertLess(prepare, source_check)
        self.assertLess(source_check, self.stage.index('tools\\merge_gn_args.py', prepare))
        self.assertIn('$VerifyRestoredSource = -not $RestoredUpstream', self.stage)
        self.assertNotIn('& "$PSScriptRoot\\update-restored-source.ps1"', self.stage)
        self.assertIn('if (Test-Path $readyMarker) { $applyArgs += "--check" }', self.restored_prep)
        self.assertIn('Assert-PreparedLayers', self.prepare)
        self.assertIn('prepared source key is $preparedKey, expected $versionKey', self.prepare)
        self.assertIn('domain substitution marker does not match the pinned core commit', self.prepare)

    def test_restore_prep_verifies_then_appends_without_replaying_upstream_layers(self):
        verify = self.prepare.index('"--phase", "verify"')
        checkout = self.prepare.index('Ensure-Checkout "https://github.com/ungoogled-software/ungoogled-chromium.git"')
        restored = self.prepare.index(self.restored_prep)
        self.assertLess(verify, checkout)
        self.assertLess(checkout, restored)
        self.assertIn('"--platform-tooling", $Windows, "--platform", "windows", "--patch-bin", $RestoredPatchExe', self.restored_prep)
        self.assertIn('$RestoredPatchExe = Resolve-HostPatch', self.restored_prep)
        self.assertNotIn('downloads.py', self.restored_prep)
        self.assertNotIn('Invoke-PatchDirectory', self.restored_prep)
        self.assertNotIn('prune_binaries.py', self.restored_prep)
        self.assertNotIn('Prepare-RustToolchain', self.restored_prep)
        self.assertLess(self.restored_prep.index('Invoke-Checked $Python $applyArgs'),
                        self.restored_prep.index('Set-Marker ".chromix-source-ready"'))
        normal = self.prepare[restored + len(self.restored_prep):]
        layers = ['"utils\\downloads.py"), "unpack"', 'Invoke-PatchDirectory (Join-Path $Ungoogled "patches")',
                  'Invoke-PatchDirectory (Join-Path $Windows "patches")', '"utils\\prune_binaries.py"',
                  'Invoke-ChromixPatches $resumeChromixPatch', 'Set-Marker ".chromix-source-ready"']
        offsets = [normal.index(layer) for layer in layers]
        self.assertEqual(offsets, sorted(offsets))

    def test_same_default_out_is_used_by_merge_gn_ninja_and_packaging(self):
        self.assertIn('$OutDir = "$Src\\out\\Chromix"', self.stage)
        self.assertEqual(self.stage.count('$OutDir = "$Src\\out\\Default"'), 2)
        self.assertNotIn('import_upstream_cache.py', self.stage)
        self.assertNotIn('$ImportUpstreamCache', self.stage)
        self.assertNotIn('Rename-Item', self.stage)
        merge = self.stage[self.stage.index('$gnArgs = Join-Path $OutDir "args.gn"'):
                           self.stage.index('if ($LASTEXITCODE -ne 0) { throw "GN argument merge failed" }')]
        self.assertIn('$mergeArgs = @((Join-Path $Repo "tools\\merge_gn_args.py"), $gnArgs)', merge)
        self.assertIn('if ($RestoredUpstream) { $mergeArgs += $gnArgs }', merge)
        inputs = ['$mergeArgs += $gnArgs', '(Join-Path $UngoogledTooling "flags.gn")',
                  '(Join-Path $WindowsTooling "flags.windows.gn")', '(Join-Path $Repo "build\\args.windows.gn")',
                  'python @mergeArgs']
        offsets = [merge.index(value) for value in inputs]
        self.assertEqual(offsets, sorted(offsets))
        self.assertIn('$gn = Join-Path $OutDir "gn.exe"', self.stage)
        self.assertIn('if (-not (Test-Path $gn)) {', self.stage)
        selected = self.stage.index('$Ninja = & python (Join-Path $Repo "tools\\restore_ninja.py")')
        self.assertIn('--workdir $WorkDir --platform windows --arch $Arch', self.stage[selected:])
        self.assertIn('if ($LASTEXITCODE -ne 0 -or -not $Ninja) { throw "restored Ninja compatibility check failed" }',
                      self.stage)
        self.assertIn('$env:NINJA = $Ninja', self.stage)
        generated = self.stage.index('& $gn gen $OutDir --fail-on-unused-args')
        planned = self.stage.index('& $Ninja -C $OutDir -n chrome')
        built = self.stage.index('$rc = Invoke-Tracked -File $Ninja')
        packaged = self.stage.index('& "$PSScriptRoot\\package-win.ps1" -Out $OutDir')
        self.assertLess(selected, self.stage.index('python tools\\rust\\build_bindgen.py'))
        self.assertLess(selected, generated)
        self.assertIn('$validationRc = Invoke-Tracked -File $Ninja', self.stage)
        self.assertLess(generated, planned)
        self.assertLess(planned, built)
        self.assertLess(built, packaged)
        self.assertLess(packaged, self.stage.index('\n  Verify-FinalBundle\n', packaged))
        self.assertNotRegex(self.cache, r'Set-Content|Set-Marker|Copy-Item|Move-Item|Expand-Archive')

    def test_required_miss_timeout_and_budget_fail_before_preparation(self):
        self.assertIn('throw "required upstream cache fetch timed out; $(Get-UpstreamTimeoutSummary)"', self.cache)
        self.assertNotIn('continuing with normal source preparation', self.cache)
        self.assertIn('restore receipt missing after restore', self.cache)
        self.assertIn('throw "upstream restore helper failed (exit $LASTEXITCODE)"', self.cache)
        self.assertIn('throw "required upstream cache: insufficient stage budget for restore"', self.cache)
        self.assertIn('$fetchTimeoutSec = [Math]::Min(10800, ((Get-RemainingMin) - $PackReserveMin - 30) * 60)', self.cache)
        self.assertIn('if ($fetchTimeoutSec -lt 60)', self.cache)
        self.assertIn('-ArgList $fetchCommandLine -Cwd $Repo -TimeoutSec $fetchTimeoutSec', self.cache)
        self.assertIn('$StageMinutes = if ($ValidateOnly) { 230 } else { 300 }', self.stage)
        self.assertIn('$PackReserveMin = if ($ValidateOnly) { 15 } else { 40 }', self.stage)

    def test_workflow_requires_cache_in_validation_and_every_resume(self):
        import yaml

        workflow = yaml.safe_load(self.workflow)
        required = "${{ (inputs.use_upstream_cache || inputs.upstream_run_id != '') && '1' || '0' }}"
        self.assertEqual(workflow['env']['CHROMIX_USE_UPSTREAM_CACHE'], required)
        self.assertEqual(workflow['env']['CHROMIX_PREFER_UPSTREAM_CACHE'],
                         "${{ github.event_name == 'push' && '1' || '0' }}")
        for job in workflow['jobs'].values():
            self.assertNotIn('CHROMIX_USE_UPSTREAM_CACHE', job.get('env', {}))
            for step in job['steps']:
                self.assertNotIn('CHROMIX_USE_UPSTREAM_CACHE', step.get('env', {}))
        self.assertIn('$UseUpstreamCache -or $UpstreamRunId -or ($env:CHROMIX_USE_UPSTREAM_CACHE -eq "1")',
                      self.stage)
        self.assertLess(self.stage.index('($FromArtifact -or $StageIndex -ne 1 -or (Test-Path $Src))'),
                        self.stage.index('$VerifyRestoredSource = $false'))

    def test_missing_bindgen_uses_normal_builder_with_known_endpoint_normalization(self):
        start = self.stage.index('if (-not (Test-Path "third_party\\rust-toolchain\\bin\\bindgen.exe"))')
        end = self.stage.index('  if (-not (Test-Path $domainMarker))', start)
        bindgen = self.stage[start:end]
        self.assertIn('foreach ($binary in @("cargo.exe", "rustc.exe"))', bindgen)
        self.assertIn('if ($RestoredUpstream)', bindgen)
        self.assertIn('from upstream_script_identity import ENDPOINTS, RESTORED', bindgen)
        self.assertIn('python tools\\rust\\build_bindgen.py --skip-test', bindgen)
        self.assertLess(bindgen.index('python -c $normalizeToolUrls'), bindgen.index('python tools\\rust'))

    def test_inspect_precedes_tools_and_finish_precedes_gn_without_placeholder_report(self):
        self.assertIn('"--phase", "inspect"', self.restored_prep)
        self.assertIn('"--platform", "windows", "--arch", $Arch, "--workdir", $Root', self.restored_prep)
        self.assertLess(self.restored_prep.index('prepare_restored_build.py'),
                        self.restored_prep.index('Assert-RestoredToolchain'))
        self.assertLess(self.stage.index('& "$PSScriptRoot\\prepare-ungoogled.ps1"'),
                        self.stage.index('python tools\\rust\\build_bindgen.py'))
        finish = self.stage.index('prepare_restored_build.py") --phase finish')
        self.assertLess(self.stage.index('throw "bindgen build failed"'), finish)
        self.assertLess(finish, self.stage.index('python tools\\gn\\bootstrap\\bootstrap.py'))
        self.assertLess(finish, self.stage.index('& $gn gen $OutDir'))
        self.assertNotIn('upstream-cache-preparation.json', self.stage)

    def test_user_input_and_optional_token_use_environment_not_interpolation(self):
        runs = []
        for number in range(1, 13):
            runs.extend(workflow_runs(workflow_job(self.workflow, f"build-{number}")))
        self.assertEqual(len(runs), 61)
        for run in runs:
            self.assertNotIn("${{", run)
        self.assertIn("UPSTREAM_RUN_ID: ${{ inputs.upstream_run_id }}", self.build_one)
        self.assertIn("USE_UPSTREAM_CACHE: ${{ inputs.use_upstream_cache }}", self.build_one)
        self.assertIn("GH_TOKEN: ${{ secrets.UPSTREAM_ACTIONS_TOKEN || github.token }}", self.build_one)
        self.assertIn(r"[ValidatePattern('\A[0-9]*\z')] [string]$UpstreamRunId", self.stage)
        self.assertIn('if ($UpstreamRunId) { $fetchArgs += @("--run-id", $UpstreamRunId) }', self.cache)
        self.assertIn('$fetchCommandLine = ($fetchArgs | ForEach-Object { "`"$_`"" }) -join " "', self.cache)
        self.assertNotIn('Invoke-Expression', self.stage)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh is not installed")
    def test_powershell_scripts_and_workflow_commands_parse_without_execution(self):
        commands = []
        for number in range(1, 13):
            commands.extend(workflow_runs(workflow_job(self.workflow, f"build-{number}")))
        commands.extend([self.stage, self.prepare])
        parser = r'''
$tokens = $null
$errors = $null
[Management.Automation.Language.Parser]::ParseInput($env:PS_INPUT, [ref]$tokens, [ref]$errors) | Out-Null
if ($errors.Count) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }
'''
        for code in commands:
            result = subprocess.run([shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", parser],
                                    env={**os.environ, "PS_INPUT": code}, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class WindowsStageBudgetTest(unittest.TestCase):
    def setUp(self):
        self.powershell = shutil.which("pwsh") or "/opt/pwsh/pwsh"
        if not Path(self.powershell).is_file():
            self.skipTest("pwsh is unavailable")
        self.stage = STAGE.read_text()

    def run_ps(self, code):
        return subprocess.run([self.powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                               '$ErrorActionPreference = "Stop"\n' + code],
                              capture_output=True, text=True, timeout=20)

    def test_actual_deadlines_and_reserves_fit_workflow_jobs(self):
        import yaml

        result = self.run_ps(r'''
function Get-Date { return [datetime]"2026-09-09T00:00:00Z" }
$rows = foreach ($ValidateOnly in @($true, $false)) {
''' + stage_budget_source() + r'''
  @{ validate = $ValidateOnly; minutes = ($Deadline - (Get-Date)).TotalMinutes; reserve = $PackReserveMin }
}
ConvertTo-Json -Compress -InputObject @($rows)
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual(rows, [{"validate": True, "minutes": 230, "reserve": 15},
                                {"validate": False, "minutes": 300, "reserve": 40}])
        jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
        self.assertNotIn("validate", jobs)
        self.assertTrue(all(jobs[f"build-{number}"]["timeout-minutes"] == 355 for number in range(1, 13)))
        self.assertLess(rows[0]["minutes"], jobs["build-1"]["timeout-minutes"])
        self.assertLess(rows[1]["minutes"], jobs["build-1"]["timeout-minutes"])

    def test_remaining_minutes_floors_instead_of_rounding_up(self):
        start = self.stage.index("function Get-RemainingMin {")
        end = self.stage.index("function Test-LastStage", start)
        result = self.run_ps(r'''
function Get-Date { return [datetime]"2026-09-09T00:00:00Z" }
''' + self.stage[start:end] + r'''
$values = foreach ($seconds in @(119, 120, 59, -1)) {
  $Deadline = (Get-Date).AddSeconds($seconds)
  Get-RemainingMin
}
ConvertTo-Json -Compress -InputObject @($values)
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [1, 2, 0, -1])

    def test_actual_fetch_formula_preserves_reserve_and_preparation_margin(self):
        start = self.stage.index("  $fetchTimeoutSec =")
        end = self.stage.index("  $fetchArgs =", start)
        result = self.run_ps(r'''
function Get-RemainingMin { return $left }
$rows = foreach ($ValidateOnly in @($true, $false)) {
''' + stage_budget_source() + r'''
  foreach ($left in @(300, 251, 250, 249, 230, 226, 225, 224, 220, 140, 71, 70, 46, 45, 0, -1)) {
    try {
''' + self.stage[start:end] + r'''
      @{ validate = $ValidateOnly; left = $left; reserve = $PackReserveMin; seconds = $fetchTimeoutSec }
    } catch {
      @{ validate = $ValidateOnly; left = $left; reserve = $PackReserveMin; error = $_.Exception.Message }
    }
  }
}
ConvertTo-Json -Compress -InputObject @($rows)
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        for row in json.loads(result.stdout):
            expected = min(10800, (row["left"] - row["reserve"] - 30) * 60)
            if expected < 60:
                self.assertIn("insufficient stage budget", row["error"])
            else:
                self.assertEqual(row["seconds"], expected)
                self.assertLessEqual(row["seconds"] / 60 + row["reserve"] + 30, row["left"])

    def test_initial_validation_extraction_budget_is_about_175_minutes_after_setup(self):
        start = self.stage.index("  $fetchTimeoutSec =")
        end = self.stage.index("  $fetchArgs =", start)
        result = self.run_ps(r'''
function Get-Date { return [datetime]"2026-09-09T00:00:00Z" }
$ValidateOnly = $true
''' + stage_budget_source() + r'''
function Get-RemainingMin { return $StageMinutes - 10 }
''' + self.stage[start:end] + r'''
Write-Output $fetchTimeoutSec
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(int(result.stdout), 175 * 60)

    def test_actual_torque_call_is_bounded_and_insufficient_time_fails(self):
        start = self.stage.rindex("\nif ($ValidateOnly -or ($StageIndex -eq 1 -and -not $FromArtifact)) {")
        end = self.stage.index("\n$ninjaBudget =", start)
        for left, rc in ((15, 0), (14, 0), (0, 0), (16, 0), (35, 0), (139, 0), (35, 124)):
            with self.subTest(left=left, rc=rc):
                result = self.run_ps(f'$left = {left}\n$rc = {rc}\n' + r'''
$ValidateOnly = $true
$Src = "/fixture/src"
$OutDir = "/fixture/src/out/Default"
$Ninja = "/fixture/selected tools/ninja.exe"
function Get-RemainingMin { return $left }
function Invoke-Tracked {
  param($File, $ArgList, $Cwd, $TimeoutSec, [switch]$FullFailureOutput)
  if ($File -ne $Ninja) { throw "validation did not use selected Ninja" }
  Write-Host "timeout:$TimeoutSec"
  return $rc
}
function Write-OutVar($key, $value) { Write-Host "$key=$value" }
''' + stage_budget_source() + self.stage[start:end])
                if left <= 15:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("insufficient V8 Torque budget", result.stderr)
                    self.assertNotIn("timeout:", result.stdout)
                else:
                    self.assertIn(f"timeout:{(left - 15) * 60}", result.stdout)
                    self.assertEqual(result.returncode == 0, rc == 0, result.stderr)
                self.assertEqual("finished=true" in result.stdout, left > 15 and rc == 0)


class WindowsRequiredCacheTest(unittest.TestCase):
    def setUp(self):
        from tools.tests.test_posix_ci_stage import FullCacheFixture

        self.fixture = FullCacheFixture()
        self.fixture.addCleanup = self.addCleanup
        self.fixture.setUp()
        self.powershell = shutil.which("pwsh") or "/opt/pwsh/pwsh"
        if not Path(self.powershell).is_file():
            self.skipTest("pwsh is unavailable")
        self.fixture.pin_repo = self.fixture.repo
        for relative in ("CHROMIUM_VERSION", "CHROMIUM_LINUX_VERSION", "CHROMIUM_MACOS_VERSION",
                         "CHROMIUM_WINDOWS_VERSION", "build/ungoogled-revisions.psd1", "build/upstream-cache.json",
                         "build/windows/read-platform-pins.ps1"):
            self.fixture.put(self.fixture.repo / relative, (REPO / relative).read_bytes())
        for source in (REPO / "tools").glob("*.py"):
            self.fixture.put(self.fixture.repo / "tools" / source.name, source.read_bytes())
        path = self.fixture.repo / "build/upstream-cache.json"
        manifest = json.loads(path.read_text())
        manifest["sources"]["windows"] = windows_source_fixture(self.fixture.repo)
        path.write_text(json.dumps(manifest))
        self.fixture.put(self.fixture.repo / 'build/windows/assert-target-arch.ps1',
                         (REPO / 'build/windows/assert-target-arch.ps1').read_bytes())
        stage = STAGE.read_text()
        policy = next(line for line in stage.splitlines() if line.startswith("$RequireUpstreamCache ="))
        start = stage.index('# Cache restores establish the marker')
        end = stage.index('\nif (-not (Test-Path (Join-Path $Src ".chromix-source-ready"))', start)
        self.script = self.fixture.repo / 'build/windows/stage.ps1'
        self.script.write_text(r'''
$ErrorActionPreference = "Stop"
$Repo = $env:TEST_REPO
$WorkDir = $env:TEST_WORK
$Src = Join-Path $WorkDir "src"
$OutDir = Join-Path $Src "out/Chromix"
$UpstreamCacheDir = $env:TEST_CACHE
$Revisions = & (Join-Path $Repo "build/windows/read-platform-pins.ps1") -Repo $Repo
$RestoredUpstream = $false
$StageIndex = [int]$env:TEST_STAGE
$Arch = $env:TEST_ARCH
$FromArtifact = $env:TEST_RESUME -eq "1"
$UseUpstreamCache = $env:TEST_SWITCH -eq "1"
$UpstreamRunId = $env:TEST_RUN_ID
$ValidateOnly = $env:TEST_VALIDATE -eq "1"
function Get-RemainingMin { return [int]$env:TEST_MINUTES }
function Invoke-Tracked {
  param($File, $ArgList, $Cwd, $TimeoutSec)
  if (-not $ArgList.Contains('"--arch" "' + $Arch + '"')) { throw "fetch target architecture missing" }
  if ($UpstreamRunId -and -not $ArgList.Contains('"--run-id" "' + $UpstreamRunId + '"')) {
    throw "explicit upstream run missing"
  }
  Add-Content $env:CALL_LOG "fetch"
  if ($env:TEST_TRACKED_ERROR) { throw $env:TEST_TRACKED_ERROR }
  if ($TimeoutSec -gt 10800 -or $TimeoutSec -lt 60 -or
      $TimeoutSec -gt ((Get-RemainingMin) - $PackReserveMin - 30) * 60) { throw "unbounded fetch" }
  return [int]$env:FETCH_RC
}
function python {
  Add-Content $env:CALL_LOG $args[2]
  & $env:TEST_PYTHON @args
  $global:LASTEXITCODE = $LASTEXITCODE
}
''' + stage_budget_source() + policy + "\n" +
                               stage[stage.index("function Get-UpstreamTimeoutSummary {"):
                                     stage.index("function Invoke-Tracked {")] + stage[start:end] + r'''
Add-Content $env:CALL_LOG "prepare"
Add-Content $env:CALL_LOG ("ninja:" + $OutDir.Replace('\', '/'))
''')

    def run_stage(self, *, enabled=True, validate=False, resume=False, stage=1,
                  minutes=300, fetch_rc=0, switch=False, run_id="", tracked_error="", prefer=False, arch="x64"):
        fixture = self.fixture
        env = {**fixture.env, "TEST_REPO": str(fixture.repo), "TEST_WORK": str(fixture.work),
               "TEST_CACHE": str(fixture.cache), "TEST_PYTHON": sys.executable,
               "TEST_STAGE": str(stage), "TEST_RESUME": str(int(resume)), "TEST_ARCH": arch,
               "TEST_SWITCH": str(int(switch)), "TEST_RUN_ID": run_id,
               "TEST_VALIDATE": str(int(validate)), "TEST_MINUTES": str(minutes),
               "FETCH_RC": str(fetch_rc), "TEST_TRACKED_ERROR": tracked_error,
               "CHROMIX_USE_UPSTREAM_CACHE": str(int(enabled)),
               "CHROMIX_PREFER_UPSTREAM_CACHE": str(int(prefer))}
        return subprocess.run([self.powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                               "-File", str(self.script)], env=env, capture_output=True, text=True, timeout=20)

    def test_arm64_restore_initializes_verified_marker_and_resumes_same_target(self):
        self.fixture.seed('windows', 'arm64')
        result = self.run_stage(arch='arm64', run_id='103')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.fixture.work / '.chromix-target-arch').read_text().strip(), 'arm64')
        self.assertEqual(self.fixture.called()[:4], ['fetch', 'restore', 'verify', 'verify'])
        receipt = json.loads((self.fixture.work / 'src/.chromix-upstream-restored.json').read_text())
        self.assertEqual(receipt['identity']['arch'], 'arm64')
        self.assertEqual(receipt['identity']['run_id'], 103)
        self.fixture.calls.unlink()
        resumed = self.run_stage(arch='arm64', stage=2, resume=True)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(self.fixture.called(), ['verify', 'prepare',
                         f'ninja:{self.fixture.work.as_posix()}/src/out/Default'])
        (self.fixture.work / '.chromix-target-arch').unlink()
        self.fixture.calls.unlink()
        missing = self.run_stage(arch='arm64', stage=2, resume=True)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn('no target architecture marker', missing.stderr)
        self.assertFalse((self.fixture.work / '.chromix-target-arch').exists())
        self.assertEqual(self.fixture.called(), [])

    def test_arm64_rejects_x64_cache_without_initializing_marker(self):
        self.fixture.seed('windows', 'x64')
        result = self.run_stage(arch='arm64')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.fixture.work / '.chromix-target-arch').exists())
        self.assertNotIn('prepare', self.fixture.called())

    def test_arm64_rejects_x64_snapshot_even_with_arm64_marker(self):
        self.fixture.seed('windows', 'x64')
        restored = self.run_stage()
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.fixture.calls.unlink()
        self.fixture.put(self.fixture.work / '.chromix-target-arch', 'arm64')
        result = self.run_stage(arch='arm64', stage=2, resume=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('receipt platform/architecture mismatch', result.stderr)
        self.assertEqual(self.fixture.called(), [])

    def test_arm64_default_is_cold_and_explicit_miss_stops_before_marker_or_prepare(self):
        self.fixture.seed('windows', 'arm64', reason='artifact_expired')
        failed = self.run_stage(arch='arm64')
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse((self.fixture.work / '.chromix-target-arch').exists())
        self.assertEqual(self.fixture.called(), ['fetch'])
        self.fixture.calls.unlink()
        cold = self.run_stage(arch='arm64', enabled=False)
        self.assertEqual(cold.returncode, 0, cold.stderr)
        self.assertEqual(self.fixture.called(), ['prepare',
                         f'ninja:{self.fixture.work.as_posix()}/src/out/Chromix'])
        self.assertEqual((self.fixture.work / '.chromix-target-arch').read_text().strip(), 'arm64')

    def test_only_metadata_expiry_can_fall_back_when_cache_is_optional(self):
        for enabled, phase, reason, success in (
                (False, 'metadata', 'artifact_expired', True),
                (True, 'metadata', 'artifact_expired', False),
                (False, 'download', 'artifact_expired', False),
                (False, 'metadata', 'digest_mismatch', False),
                (False, 'metadata', 'unavailable', False),
                (False, 'extract_source_and_objects', 'insufficient_disk_space', False)):
            with self.subTest(enabled=enabled, phase=phase, reason=reason):
                self.fixture.seed('windows', 'x64', reason=reason)
                path = self.fixture.cache / 'result.json'
                report = json.loads(path.read_text())
                report.update(phase=phase, duration_seconds=1)
                path.write_text(json.dumps(report))
                result = self.run_stage(enabled=enabled, prefer=True)
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                if success:
                    self.assertIn('pinned cold-source preparation', result.stdout)
                    self.assertEqual(self.fixture.called(), ['fetch', 'prepare',
                        f'ninja:{self.fixture.work.as_posix()}/src/out/Chromix'])
                else:
                    self.assertEqual(self.fixture.called(), ['fetch'])
                self.assertFalse((self.fixture.work / 'src').exists())
                self.fixture.calls.unlink()

    def test_required_miss_disk_shortage_and_timeout_fail_in_validation_too(self):
        for validate in (False, True):
            for reason in ("unavailable", "insufficient_disk_space", "cache_timeout", "download_timeout"):
                with self.subTest(validate=validate, reason=reason):
                    self.fixture.seed("windows", "x64", reason=reason)
                    result = self.run_stage(validate=validate)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(f"required upstream cache fetch failed: {reason}", result.stderr)
                    self.assertEqual(json.loads((self.fixture.cache / "result.json").read_text())["reason"], reason)
                    self.assertEqual(self.fixture.called(), ["fetch"])
                    self.assertFalse((self.fixture.work / "src").exists())
                    self.fixture.calls.unlink()

    def test_download_timeout_reports_phase_and_duration_without_attempting_restore(self):
        self.fixture.seed("windows", "x64", reason="download_timeout")
        report = self.fixture.cache / "result.json"
        result = json.loads(report.read_text())
        result.update(phase="download", duration_seconds=901.657)
        report.write_text(json.dumps(result))
        failed = self.run_stage(validate=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("required upstream cache fetch failed: download_timeout", failed.stderr)
        self.assertIn("phase=download", failed.stderr)
        self.assertIn("duration_seconds=901.657", failed.stderr)
        self.assertNotIn("restore receipt missing", failed.stderr)
        self.assertEqual(self.fixture.called(), ["fetch"])
        self.assertFalse((self.fixture.work / "src").exists())

    def test_tracked_timeout_reports_only_safe_progress_fields_before_restore(self):
        report = self.fixture.cache / "result.json"
        for elapsed in ({"duration_seconds": 3600.25}, {"elapsed_seconds": 3600.25, "duration_seconds": 0}):
            with self.subTest(elapsed=elapsed):
                self.fixture.put(report, json.dumps({"phase": "extract_source_and_objects", "members": 1234,
                                                    "extracted_bytes": 15011844651, **elapsed,
                                                    "manifest": {"token": "SECRET_DO_NOT_PRINT"}}))
                result = self.run_stage(validate=True, fetch_rc=124, minutes=220)
                self.assertNotEqual(result.returncode, 0)
                combined = result.stdout + result.stderr
                for text in ("required upstream cache fetch timed out", "phase=extract_source_and_objects",
                             "members=1234", "extracted_bytes=15011844651", "elapsed_seconds=3600.25"):
                    self.assertIn(text, combined)
                self.assertNotIn("SECRET_DO_NOT_PRINT", combined)
                self.assertNotIn("manifest", combined)
                self.assertEqual(self.fixture.called(), ["fetch"])
                self.assertFalse((self.fixture.work / "src").exists())
                self.fixture.calls.unlink()

    def test_failed_tracked_cleanup_preserves_error_and_reports_phase(self):
        self.fixture.put(self.fixture.cache / "result.json", json.dumps({"phase": "extract_source_and_objects",
                                                                       "members": 51, "duration_seconds": 3600}))
        message = "tracked process is still running after taskkill; refusing safe snapshot"
        result = self.run_stage(validate=True, tracked_error=message)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tracked process is still running after taskkill", result.stderr)
        self.assertIn("phase=extract_source_and_objects", result.stdout)
        self.assertIn("members=51", result.stdout)
        self.assertIn("elapsed_seconds=3600", result.stdout)
        self.assertEqual(self.fixture.called(), ["fetch"])
        self.assertFalse((self.fixture.work / "src").exists())

    def test_timeout_summary_tolerates_missing_partial_oversized_and_unsafe_reports(self):
        report = self.fixture.cache / "result.json"
        cases = (None, "not-json SECRET_DO_NOT_PRINT", "{}", "null",
                 json.dumps({"phase": "extract\nSECRET_DO_NOT_PRINT", "members": {"token": "SECRET_DO_NOT_PRINT"},
                             "extracted_bytes": -1, "elapsed_seconds": "SECRET_DO_NOT_PRINT"}),
                 json.dumps({"phase": "SECRET_DO_NOT_PRINT", "padding": "x" * 65536}))
        for content in cases:
            with self.subTest(content=None if content is None else content[:80]):
                report.unlink(missing_ok=True)
                if content is not None:
                    self.fixture.put(report, content)
                result = self.run_stage(validate=True, fetch_rc=124)
                self.assertNotEqual(result.returncode, 0)
                combined = result.stdout + result.stderr
                self.assertIn("required upstream cache fetch timed out", combined)
                for key in ("phase", "members", "extracted_bytes", "elapsed_seconds"):
                    self.assertIn(f"{key}=unknown", combined)
                self.assertNotIn("SECRET_DO_NOT_PRINT", combined)
                self.assertEqual(self.fixture.called(), ["fetch"])
                self.fixture.calls.unlink()

    def test_missing_invalid_or_incomplete_fetch_report_fails_before_restore(self):
        for content in (None, "not json", "{}", "null"):
            with self.subTest(content=content):
                if content is not None:
                    self.fixture.put(self.fixture.cache / "result.json", content)
                result = self.run_stage(validate=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.fixture.called(), ["fetch"])
                self.assertFalse((self.fixture.work / "src").exists())
                self.fixture.calls.unlink()

    def test_required_budget_and_fetch_errors_do_not_reach_prepare(self):
        for minutes, rc, calls in ((45, 0, []), (140, 124, ["fetch"]), (140, 7, ["fetch"])):
            with self.subTest(minutes=minutes, rc=rc):
                result = self.run_stage(minutes=minutes, fetch_rc=rc, validate=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.fixture.called(), calls)
                if self.fixture.calls.exists():
                    self.fixture.calls.unlink()

    def test_validate_hit_and_low_budget_resume_verify_and_keep_default_out(self):
        self.fixture.seed("windows", "x64")
        result = self.run_stage(validate=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected = ["prepare", f"ninja:{self.fixture.work.as_posix()}/src/out/Default"]
        self.assertEqual(self.fixture.called(), ["fetch", "restore", "verify"] + expected)
        self.fixture.calls.unlink()
        result = self.run_stage(stage=2, resume=True, minutes=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fixture.called(), ["verify"] + expected)
        self.assertEqual((self.fixture.work / "src/out/Default/obj/retained.o").read_bytes(), b"tiny cached object")

    def test_cold_and_forged_resume_receipts_fail_before_preparation(self):
        src = self.fixture.work / "src"
        self.fixture.put(src / ".chromix-source-ready", "old-version|ready")
        for receipt in (None, "{}"):
            with self.subTest(receipt=receipt):
                if receipt is not None:
                    self.fixture.put(src / ".chromix-upstream-restored.json", receipt)
                result = self.run_stage(stage=2, resume=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.fixture.called(), [])
                if receipt is not None:
                    self.assertIn('receipt platform/architecture mismatch', result.stderr)
                self.assertTrue((src / ".chromix-source-ready").is_file())

    def test_fresh_no_cache_is_normal_but_switch_or_run_id_requires_it(self):
        result = self.run_stage(enabled=False, validate=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fixture.called(), ["prepare", f"ninja:{self.fixture.work.as_posix()}/src/out/Chromix"])
        self.fixture.calls.unlink()
        for option in ({"switch": True}, {"run_id": "123"}):
            with self.subTest(option=option):
                self.fixture.seed("windows", "x64", reason="unavailable")
                result = self.run_stage(enabled=False, **option)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.fixture.called(), ["fetch"])
                self.fixture.calls.unlink()


class WindowsRestoredPreparationFixture:
    arch = "x64"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="windows restored prep ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.work = self.root / "work"
        self.src = self.work / "src"
        self.repo.mkdir()
        self.src.mkdir(parents=True)
        self.calls = self.root / "calls"
        for relative in ("CHROMIUM_VERSION", "CHROMIUM_LINUX_VERSION", "CHROMIUM_MACOS_VERSION",
                         "CHROMIUM_WINDOWS_VERSION", "build/ungoogled-revisions.psd1", "build/upstream-cache.json",
                         "build/windows/read-platform-pins.ps1"):
            self.put(self.repo / relative, (REPO / relative).read_text())
        self.pins = load_pins(self.repo, "windows")
        path = self.repo / "build/upstream-cache.json"
        manifest = json.loads(path.read_text())
        manifest["sources"]["windows"] = windows_source_fixture(self.repo)
        self.put(path, json.dumps(manifest))
        self.put(self.repo / "patches/series", "patches/one.patch\n")
        self.put(self.repo / "patches/one.patch", "diff --git a/sample.cc b/sample.cc\n--- a/sample.cc\n+++ b/sample.cc\n"
                 "@@ -1 +1 @@\n-google.test upstream\n+google.test Chromix\n")
        self.put(self.src / "sample.cc", "blocked.test upstream\n")
        self.put(self.src / ".chromix-upstream-restored.json", '{"valid": true}')
        self.put(self.src / "out/Default/obj/retained.obj", "cached object")
        for name in ("apply_restored_patches.py", "verify_patch_stack.py", "platform_pins.py"):
            self.put(self.repo / "tools" / name, (REPO / "tools" / name).read_text())
        self.put(self.repo / "tools/restore_upstream_cache.py", '''import json, os, sys
from pathlib import Path
with open(os.environ['MOCK_CALLS'], 'a') as output:
    output.write('verify\\n')
assert sys.argv[1:7] == ['--phase', 'verify', '--platform', 'windows', '--arch', os.environ['CHROMIX_TARGET_ARCH']]
assert '--cache-dir' not in sys.argv
src = Path(sys.argv[sys.argv.index('--workdir') + 1]) / 'src'
assert json.loads((src / '.chromix-upstream-restored.json').read_text()).get('valid')
''')
        core = self.work / "tooling/ungoogled-chromium"
        windows = self.work / "tooling/ungoogled-chromium-windows"
        self.put(core / "chromium_version.txt", self.pins["ChromiumVersion"])
        self.put(core / "revision.txt", "1")
        self.put(windows / "revision.txt", "1")
        self.put(core / "domain_regex.list", r"google\.test#blocked.test" + "\n")
        self.put(windows / "domain_substitution.list", "sample.cc\n")
        source = PREPARE.read_text()
        toolcheck = source[source.index('function Assert-RestoredToolchain'):source.index('function Assert-PreparedLayers')]
        for relative in re.findall(r'^    "(third_party[^"\n]+)"[,]?$', toolcheck, re.M):
            self.put(self.src / relative.replace('\\', '/'), "tool fixture")
        for name in ("libstd-fixture.rlib", "libcore-fixture.rlib", "liballoc-fixture.rlib", "libcompiler_builtins-fixture.rlib"):
            self.put(self.src / "third_party/rust-toolchain/lib/rustlib/x86_64-pc-windows-msvc/lib" / name, "std")
        self.put(self.src / "third_party/rust-toolchain/bin/rustc_driver-fixture.dll", "driver")
        for relative in ("include/stddef.h", "include/stdarg.h", "lib/windows/clang_rt.builtins-x86_64.lib"):
            self.put(self.src / "third_party/llvm-build/Release+Asserts/lib/clang/22" / relative, "resource")
        self.put(self.repo / 'build/windows/prep_rust_toolchain.py',
                 (REPO / 'build/windows/prep_rust_toolchain.py').read_text())
        self.put(self.root / 'assert-target-arch.ps1', (REPO / 'build/windows/assert-target-arch.ps1').read_text())
        if self.arch == 'arm64':
            self.put(self.work / '.chromix-target-arch', 'arm64')
            for name in ('std', 'core', 'alloc', 'compiler_builtins'):
                self.put(self.src / f'third_party/rust-toolchain/lib/rustlib/aarch64-pc-windows-msvc/lib/lib{name}-fixture.rlib', 'std')
        self.prepare_build_fixture()
        # Only host executable discovery differs on Linux; the preparation body is unchanged.
        source = source.replace('(Get-Command python.exe -ErrorAction Stop).Source', '$env:MOCK_PYTHON')
        source = source.replace('Get-Command patch.exe -ErrorAction SilentlyContinue', 'Get-Command patch -ErrorAction SilentlyContinue')
        self.script = self.root / "prepare.ps1"
        self.put(self.script, source)
        self.put(self.root / "read-platform-pins.ps1",
                 (self.repo / "build/windows/read-platform-pins.ps1").read_text())
        self.wrapper = self.root / "run.ps1"
        self.put(self.wrapper, r'''
$ErrorActionPreference = "Stop"
function git {
  Add-Content -LiteralPath $env:MOCK_CALLS -Value ("git " + ($args -join " "))
  if ($args[0] -eq "clone") {
    New-Item -ItemType Directory -Force -Path (Join-Path $args[-1] ".git") | Out-Null
  }
  if ($args[0] -eq "-C") {
    if ($args[1] -like "*ungoogled-chromium-windows") { $env:MOCK_WINDOWS } else { $env:MOCK_CORE }
  }
  $global:LASTEXITCODE = 0
}
& $env:MOCK_SCRIPT -Root $env:MOCK_WORK -Repo $env:MOCK_REPO
''')
        self.env = {**os.environ, 'CHROMIX_TARGET_ARCH': self.arch,
                    "MOCK_CALLS": str(self.calls), "MOCK_PYTHON": sys.executable,
                    "MOCK_CORE": self.pins["UngoogledCommit"], "MOCK_WINDOWS": self.pins["UngoogledWindowsCommit"],
                    "MOCK_SCRIPT": str(self.script), "MOCK_WORK": str(self.work), "MOCK_REPO": str(self.repo)}

    @staticmethod
    def put(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")

    def prepare_build_fixture(self):
        from tools import prepare_restored_build as helper
        from tools import restore_upstream_cache as restore

        identity, _, manifest = restore.identities(self.repo, "windows", self.arch)
        version = "\n".join(f"{key}={value}" for key, value in zip(
            ("MAJOR", "MINOR", "BUILD", "PATCH"), identity["chromium_version"].split(".")))
        self.put(self.src / "chrome/VERSION", version)
        self.put(self.src / "BUILD.gn", "# fixture\n")
        self.out = self.src / "out/Default"
        self.put(self.out / "args.gn", f'target_cpu = "{self.arch}"\nis_debug = true\n'
                 'chrome_pgo_phase = 0\nchrome_pgo_phase = 2 # donor Windows override\n'
                 'extra_literal = ["upstream", "with spaces"]\ncommon_override = "donor"\n'
                 'windows_override = "donor"\n')
        self.put(self.out / "build.ninja", "# fixture\n")
        self.put(self.out / ".ninja_log", "# ninja log v5\n")
        self.put(self.src / "include/local.h", "local header")
        records = {"obj/retained.obj": ["../../include/local.h"],
                   "obj/sdk.obj": [r"C:\Program Files\Windows Kits\10\Include\external.h"]}
        paths = list(dict.fromkeys(path for output, inputs in records.items() for path in (output, *inputs)))
        deps = bytearray(b"# ninjadeps\n\x04\0\0\0")
        for index, path in enumerate(paths):
            payload = path.encode()
            payload += b"\0" * (-len(payload) % 4) + struct.pack("<I", ~index & 0xffffffff)
            deps += struct.pack("<I", len(payload)) + payload
        for output, inputs in records.items():
            values = [paths.index(output), 1, 0] + [paths.index(path) for path in inputs]
            payload = struct.pack(f"<{len(values)}I", *values)
            deps += struct.pack("<I", 0x80000000 | len(payload)) + payload
        (self.out / ".ninja_deps").write_bytes(deps)
        receipt = {"schema_version": 1, "owner": restore.OWNER, "status": "restored", "valid": True,
                   "extraction_scope": restore.fetcher.SOURCE_SCOPE, "identity": identity, "manifest": manifest,
                   "platform": "windows", "arch": self.arch, "external_symlink_paths": [],
                   "original_args": restore.source_args(self.src, identity)}
        self.put(self.src / restore.MARKER, json.dumps(receipt))
        self.put(self.out / "obj/sdk.obj", "external SDK object")
        for name in ("chrome.exe", "chrome.dll", "chrome_elf.dll"):
            self.put(self.out / name, "upstream final product")
        for name, relative in helper.tool_paths("windows", self.arch).items():
            if name == "bindgen":
                continue
            path = self.src / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            header = bytearray(64)
            header[:2] = b"MZ"
            struct.pack_into("<I", header, 60, 64)
            header.extend(b"PE\0\0" + struct.pack("<H", 0xAA64 if name == "gn" else 0x8664))
            path.write_bytes(header)
        self.put(self.root / "probe.py", '''import os, sys
print('fixture ' + sys.argv[1])
raise SystemExit(1 if os.environ.get('MOCK_PROBE_FAILURE') == sys.argv[1] else 0)
''')
        # Real receipt/header checks and invalidation; only native execution is a tiny stub.
        self.put(self.repo / "tools/prepare_restored_build.py", f'''import os, subprocess, sys
from functools import partial
from pathlib import Path
sys.path.insert(0, {str(REPO)!r})
from tools import prepare_restored_build as helper
helper.prepare = partial(helper.prepare, repo=Path(os.environ['MOCK_REPO']))
root = Path(os.environ['MOCK_WORK'])
paths = {{root / 'src' / path for path in helper.tool_paths('windows', 'x64').values()}}
run = subprocess.run
def probe(command, **kwargs):
    assert Path(command[0]) in paths, command
    assert len(command) == 2 and command[1] in ('--version', '/?', '/help', '--help'), command
    return run([sys.executable, {str(self.root / 'probe.py')!r}, Path(command[0]).stem, command[1]], **kwargs)
helper.host_identity = lambda: ('windows', 'x64')
helper.subprocess.run = probe
with open(os.environ['MOCK_CALLS'], 'a') as output:
    output.write(sys.argv[sys.argv.index('--phase') + 1] + '\\n')
raise SystemExit(helper.main())
''')

    def run_prep(self):
        return subprocess.run([shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.wrapper)],
                              env=self.env, capture_output=True, text=True, timeout=30)


@unittest.skipUnless(shutil.which("pwsh") and shutil.which("patch"), "PowerShell and GNU patch required")
class WindowsRestoredPreparationMockTest(WindowsRestoredPreparationFixture, unittest.TestCase):
    def test_verified_restore_applies_real_patch_then_resumes_without_source_mutation(self):
        object_file = self.src / "out/Default/obj/retained.obj"
        object_time = object_file.stat().st_mtime_ns
        result = self.run_prep()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.src / "sample.cc").read_text(), "blocked.test Chromix\n")
        self.assertEqual(self.calls.read_text().splitlines()[0], "verify")
        self.assertTrue((self.src / ".chromix-restored-patches.json").is_file())
        for name in (".chromix-source-ready", ".chromix-toolchain-ready", ".chromix-domain-substituted"):
            self.assertTrue((self.src / name).is_file())
        modified = (self.src / "sample.cc").stat().st_mtime_ns
        resumed = self.run_prep()
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn('"status": "checked"', resumed.stdout)
        self.assertEqual((self.src / "sample.cc").stat().st_mtime_ns, modified)
        self.assertEqual(self.calls.read_text().splitlines().count("inspect"), 2)
        self.assertEqual(object_file.stat().st_mtime_ns, object_time)
        self.assertFalse((self.src / "out/Chromix").exists())

    def test_invalid_receipt_or_missing_tool_cannot_stamp_ready(self):
        receipt = (self.src / ".chromix-upstream-restored.json").read_text()
        for failure in ("receipt", "tool"):
            with self.subTest(failure=failure):
                if failure == "receipt":
                    self.put(self.src / ".chromix-upstream-restored.json", '{}')
                else:
                    self.put(self.src / ".chromix-upstream-restored.json", receipt)
                    (self.src / "third_party/rust-toolchain/bin/cargo.exe").unlink()
                result = self.run_prep()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.src / ".chromix-source-ready").exists())
                self.assertEqual((self.src / "sample.cc").read_text(), "blocked.test upstream\n")

    def test_ready_marker_never_bypasses_partial_patch_or_changed_output(self):
        good = self.run_prep()
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        self.put(self.src / "sample.cc", "tampered source\n")
        tampered = self.run_prep()
        self.assertNotEqual(tampered.returncode, 0)
        self.assertIn("completed source changed", tampered.stderr)
        for marker in (".chromix-layer-in-progress", ".chromix-domain-substitution-in-progress",
                       ".chromix-restored-patches-in-progress"):
            with self.subTest(marker=marker):
                self.put(self.src / marker, "incomplete")
                failed = self.run_prep()
                self.assertNotEqual(failed.returncode, 0)
                self.assertRegex(failed.stderr, "interrupted|in progress")
                (self.src / marker).unlink()

    def test_ready_without_patch_receipt_or_layer_marker_is_rejected(self):
        good = self.run_prep()
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        patch_receipt = self.src / ".chromix-restored-patches.json"
        saved = patch_receipt.read_bytes()
        patch_receipt.unlink()
        failed = self.run_prep()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("completion marker is missing", failed.stderr)
        patch_receipt.write_bytes(saved)
        (self.src / ".chromix-ungoogled-core").unlink()
        failed = self.run_prep()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("layer marker is missing or mismatched", failed.stderr)

    def test_failed_patch_or_stale_ready_key_never_replays_upstream_preparation(self):
        self.put(self.src / "sample.cc", "unexpected base\n")
        failed = self.run_prep()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("patch failed", failed.stderr)
        self.assertFalse((self.src / ".chromix-source-ready").exists())
        self.assertTrue((self.src / ".chromix-restored-patches-in-progress").is_file())
        (self.src / ".chromix-restored-patches-in-progress").unlink()
        self.put(self.src / ".chromix-source-ready", "old pins")
        failed = self.run_prep()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("prepared source key", failed.stderr)


@unittest.skipUnless(shutil.which("pwsh") and shutil.which("patch"), "PowerShell and GNU patch required")
class WindowsRestoredBuildStageTest(WindowsRestoredPreparationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.put(self.repo / "tools/verify_patch_stack.py", (REPO / "tools/verify_patch_stack.py").read_text())
        self.put(self.repo / "tools/merge_gn_args.py", (REPO / "tools/merge_gn_args.py").read_text())
        self.put(self.repo / "tools/upstream_script_identity.py", (REPO / "tools/upstream_script_identity.py").read_text())
        self.ninja = self.src / "third_party/ninja/ninja.exe"
        self.ninja.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.src / "third_party/rust-toolchain/bin/cargo.exe", self.ninja)
        # Native host identity and PE execution are mocked; selection and log/header checks are real.
        self.put(self.repo / "tools/restore_ninja.py", f'''import os, subprocess, sys
from pathlib import Path
sys.path.insert(0, {str(REPO)!r})
from tools import restore_ninja as helper
work = Path(os.environ['MOCK_WORK'])
ninja = work / 'src/third_party/ninja/ninja.exe'
assert sys.argv[1:] == ['--workdir', str(work), '--platform', 'windows', '--arch', os.environ['CHROMIX_TARGET_ARCH']]
run = subprocess.run
def probe(command, **kwargs):
    assert command == [str(ninja), '--version'], command
    assert kwargs['cwd'] == work, kwargs
    return run([sys.executable, '-c', 'print("1.11.1")'], **kwargs)
helper.host_identity = lambda: ('windows', 'x64')
helper.subprocess.run = probe
with open(os.environ['MOCK_CALLS'], 'a') as output:
    output.write('ninja-guard\\n')
raise SystemExit(helper.main())
''')
        self.put(self.work / "tooling/ungoogled-chromium/flags.gn",
                 'common_override = "core"\nwindows_override = "core"\nis_debug = true\n')
        self.put(self.work / "tooling/ungoogled-chromium-windows/flags.windows.gn",
                 'windows_override = "windows"\nis_debug = true\n')
        self.put(self.repo / "build/args.windows.gn", (REPO / "build/args.windows.gn").read_text())
        self.put(self.repo / 'build/args.windows.arm64.gn', (REPO / 'build/args.windows.arm64.gn').read_text())
        from tools.upstream_script_identity import ENDPOINTS, RESTORED
        for relative, keys in RESTORED.items():
            self.put(self.src / relative, "\n".join(ENDPOINTS[key][0] for key in keys))
        self.put(self.src / "tools/rust/build_bindgen.py", '''import os, struct
from pathlib import Path
with open(os.environ['MOCK_CALLS'], 'a') as output:
    output.write('bindgen\\n')
assert (Path.cwd() / '.chromix-restored-build-inspection.json').is_file()
if os.environ.get('MOCK_BINDGEN_FAILURE') == '1':
    raise SystemExit(1)
header = bytearray(64)
header[:2] = b'MZ'
struct.pack_into('<I', header, 60, 64)
header.extend(b'PE\\0\\0' + struct.pack('<H', 0x8664))
rust = Path('third_party/rust-toolchain/bin')
(rust / 'bindgen.exe').write_bytes(header)
(rust / 'libclang.dll').write_text('fixture runtime')
''')
        self.put(self.src / "tools/gn/bootstrap/bootstrap.py", '''import json, os, struct, sys
from pathlib import Path
with open(os.environ['MOCK_CALLS'], 'a') as output:
    output.write('gn-bootstrap\\n')
report = json.loads((Path(os.environ['MOCK_WORK']) / 'upstream-cache-preparation.json').read_text())
assert report['phase'] == 'finish' and report['ready_for_gn']
gn = Path(sys.argv[sys.argv.index('-o') + 1])
assert not gn.exists(), 'finish must remove incompatible GN before bootstrap'
header = bytearray(64)
header[:2] = b'MZ'
struct.pack_into('<I', header, 60, 64)
header.extend(b'PE\\0\\0' + struct.pack('<H', 0x8664))
gn.write_bytes(header)
''')
        self.put(self.repo / "tools/restored_reuse_evidence.py", '''import argparse, json, os
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--phase', choices=('before', 'after'), required=True)
parser.add_argument('--workdir', type=Path, required=True)
parser.add_argument('--platform', choices=('windows',), required=True)
parser.add_argument('--arch', choices=(os.environ['CHROMIX_TARGET_ARCH'],), required=True)
parser.add_argument('--ninja', required=True)
parser.add_argument('--target', choices=('chrome',), required=True)
parser.add_argument('--exit-code', type=int)
args = parser.parse_args()
assert args.workdir == Path(os.environ['MOCK_WORK'])
selection = json.loads((args.workdir / 'upstream-cache-ninja.json').read_text())
assert args.ninja == selection['selected']['path']
calls = Path(os.environ['MOCK_CALLS'])
previous = 'torque' if os.environ['MOCK_STAGE'] == '1' else 'ninja-plan'
assert calls.read_text().splitlines()[-1] == (previous if args.phase == 'before' else 'ninja')
with calls.open('a') as output:
    output.write('evidence-' + args.phase + '\\n')
directory = args.workdir / 'upstream-reuse'
directory.mkdir(exist_ok=True)
baseline = directory / 'baseline.json'
if args.phase == 'before':
    assert args.exit_code is None
    if not baseline.exists():
        baseline.write_text(json.dumps({'stage': os.environ['MOCK_STAGE']}))
else:
    assert baseline.is_file()
    assert args.exit_code == int(os.environ['MOCK_NINJA_RC'])
    (directory / 'result.json').write_text(json.dumps({'exit_code': args.exit_code}))
print(json.dumps({'phase': args.phase}))
raise SystemExit(37 if os.environ.get('MOCK_EVIDENCE_FAILURE') == args.phase else 0)
''')
        self.put(self.root / "package-win.ps1", r'''
param($Out, $Dest)
Add-Content -LiteralPath $env:MOCK_CALLS -Value "package"
''')
        self.put(self.root / "prepare-ungoogled.ps1", self.script.read_text())
        # This harness stubs native tool execution; the real Node helper has
        # its own Windows execution tests.
        self.put(self.root / "configure-node.ps1", 'param($NodePath)\n')
        stage = STAGE.read_text()
        start = stage.index('$domainProgress = Join-Path $Src')
        body = stage[start:]
        # PE execution is stubbed on Linux; PowerShell control flow and Python helpers are real.
        body = body.replace('& $gn gen $OutDir --fail-on-unused-args',
                            'Invoke-FixtureGn gen $OutDir --fail-on-unused-args')
        plan = '& $Ninja -C $OutDir -n chrome'
        self.assertEqual(body.count(plan), 1)
        body = body.replace(plan, 'Invoke-FixtureNinja $Ninja -C $OutDir -n chrome')
        self.put(self.wrapper, self.wrapper.read_text().split('& $env:MOCK_SCRIPT', 1)[0] + r'''
$Repo = $env:MOCK_REPO
$Root = Split-Path $env:MOCK_WORK
$WorkDir = $env:MOCK_WORK
$Src = Join-Path $WorkDir "src"
$OutDir = Join-Path $Src "out/Chromix"
$RestoredUpstream = $false
$StageIndex = [int]$env:MOCK_STAGE
$Arch = $env:CHROMIX_TARGET_ARCH
$FromArtifact = $StageIndex -gt 1
$ValidateOnly = $env:MOCK_VALIDATE -eq "1"
$UseUpstreamCache = $true
$UpstreamRunId = ""
$Deadline = (Get-Date).AddMinutes(250)
$PackReserveMin = 40
$Revisions = & (Join-Path $Repo "build/windows/read-platform-pins.ps1") -Repo $Repo
function Get-RemainingMin { return [int]$env:MOCK_MINUTES }
function Save-Handoff { param($Mode); Add-Content -LiteralPath $env:MOCK_CALLS -Value "handoff:$Mode" }
function Write-OutVar($key, $value) { Write-Host "$key=$value" }
function Verify-FinalBundle { Add-Content -LiteralPath $env:MOCK_CALLS -Value "verify-bundle" }
function Invoke-Tracked {
  param($File, $ArgList, $Cwd, $TimeoutSec, [switch]$FullFailureOutput)
  if ($File -ne $Ninja -or $Cwd -ne $Src) { throw "build did not use selected Ninja/source" }
  if ($ArgList -eq "-C `"$OutDir`" -j 1 -v gen/v8/torque-generated/bit-field-asserts.cc") {
    Add-Content -LiteralPath $env:MOCK_CALLS -Value "torque"
    if (-not $ValidateOnly) { return 0 }
  } else {
    if ($ValidateOnly -or $ArgList -ne "-C `"$OutDir`" -j 4 chrome") { throw "unexpected build arguments" }
    Add-Content -LiteralPath $env:MOCK_CALLS -Value "ninja"
  }
  return [int]$env:MOCK_NINJA_RC
}
function python {
  if ($args[0] -eq "-c") {
    Add-Content -LiteralPath $env:MOCK_CALLS -Value "normalize"
  }
  $arguments = @($args)
  $arguments[0] = $arguments[0].Replace('\', '/')
  & $env:MOCK_PYTHON @arguments
  $global:LASTEXITCODE = $LASTEXITCODE
}
function Invoke-FixtureGn {
  if (-not (Test-Path $gn)) { throw "GN is missing" }
  $report = Get-Content (Join-Path $WorkDir "upstream-cache-preparation.json") -Raw | ConvertFrom-Json
  if (-not $report.ready_for_gn -or $report.phase -ne "finish") { throw "GN before finish" }
  Add-Content -LiteralPath $env:MOCK_CALLS -Value "gn-gen"
  $global:LASTEXITCODE = 0
}
function Invoke-FixtureNinja {
  $selection = Get-Content (Join-Path $WorkDir "upstream-cache-ninja.json") -Raw | ConvertFrom-Json
  if ($selection.status -ne "selected" -or $args[0] -cne $selection.selected.path -or
      $env:NINJA -cne $selection.selected.path) { throw "plan did not use selected Ninja" }
  if ($args.Count -ne 5 -or $args[1] -ne "-C" -or $args[2] -ne $OutDir -or
      $args[3] -ne "-n" -or $args[4] -ne "chrome") { throw "unexpected Ninja plan arguments" }
  Add-Content -LiteralPath $env:MOCK_CALLS -Value "ninja-plan"
  $global:LASTEXITCODE = 0
}
''' + body)
        self.env.update(MOCK_STAGE="1", MOCK_VALIDATE="0", MOCK_MINUTES="250", MOCK_NINJA_RC="0",
                        MOCK_EVIDENCE_FAILURE="")

    def phases(self):
        return [line for line in self.calls.read_text().splitlines() if not line.startswith("git ")]

    def report(self):
        return json.loads((self.work / "upstream-cache-preparation.json").read_text())

    def test_missing_bindgen_then_finish_invalidates_before_gn_and_resume_reinspects(self):
        first = self.run_prep()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.phases(), ["verify", "verify", "inspect", "ninja-guard", "normalize", "bindgen", "finish",
                                         "gn-bootstrap", "gn-gen", "ninja-plan", "torque", "evidence-before", "ninja",
                                         "evidence-after", "package", "verify-bundle"])
        evidence = self.work / "upstream-reuse"
        baseline = evidence / "baseline.json"
        baseline_bytes = baseline.read_bytes()
        baseline_time = baseline.stat().st_mtime_ns
        self.assertEqual(json.loads((evidence / "result.json").read_text()), {"exit_code": 0})
        report = self.report()
        self.assertEqual((report["platform"], report["arch"], report["phase"]), ("windows", self.arch, "finish"))
        self.assertTrue(report["ready_for_gn"])
        self.assertGreater(report["counters"]["toolchain_invalidated_outputs"], 0)
        self.assertFalse((self.out / "obj/retained.obj").exists())
        self.assertFalse((self.out / "obj/sdk.obj").exists())
        for name in ("chrome.exe", "chrome.dll", "chrome_elf.dll"):
            self.assertFalse((self.out / name).exists())
        for name in ("args.gn", "build.ninja", ".ninja_deps", ".ninja_log"):
            self.assertTrue((self.out / name).is_file())
        self.assertFalse((self.src / ".chromix-restored-build-inspection.json").exists())
        args = (self.out / "args.gn").read_text()
        self.assertIn('extra_literal = ["upstream", "with spaces"]', args)
        self.assertIn('common_override = "core"', args)
        self.assertIn('windows_override = "windows"', args)
        self.assertEqual(args.count('is_debug ='), 1)
        self.assertIn('is_debug = false', args)
        self.assertIn(f'target_cpu = "{self.arch}"', args)
        self.assertIn('chrome_pgo_phase = ' + ('2 # donor Windows override' if self.arch == 'arm64' else '0'), args)
        self.assertEqual(args.count('chrome_pgo_phase'), 1)
        self.assertFalse((self.src / "out/Chromix").exists())
        self.put(self.out / "obj/retained.obj", "new object")
        self.put(self.out / "chrome.exe", "Chromix product")
        times = {name: (self.out / name).stat().st_mtime_ns for name in ("obj/retained.obj", "chrome.exe", "gn.exe")}
        self.calls.unlink()
        self.env["MOCK_STAGE"] = "2"
        resumed = self.run_prep()
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(self.phases(), ["verify", "verify", "inspect", "ninja-guard", "finish", "gn-gen", "ninja-plan",
                                         "evidence-before", "ninja", "evidence-after", "package", "verify-bundle"])
        self.assertEqual(self.report()["counters"]["toolchain_invalidated_outputs"], 0)
        self.assertEqual(times, {name: (self.out / name).stat().st_mtime_ns for name in times})
        self.assertEqual((self.out / "args.gn").read_text(), args)
        self.assertEqual(baseline.read_bytes(), baseline_bytes)
        self.assertEqual(baseline.stat().st_mtime_ns, baseline_time)
        self.calls.unlink()
        self.env["MOCK_STAGE"] = "8"
        resumed_stage8 = self.run_prep()
        self.assertEqual(resumed_stage8.returncode, 0, resumed_stage8.stdout + resumed_stage8.stderr)
        self.assertEqual(self.phases(), ["verify", "verify", "inspect", "ninja-guard", "finish", "gn-gen", "ninja-plan",
                                         "evidence-before", "ninja", "evidence-after", "package", "verify-bundle"])
        self.assertEqual(times, {name: (self.out / name).stat().st_mtime_ns for name in times})
        self.assertEqual((self.out / "args.gn").read_text(), args)
        self.assertEqual((baseline.read_bytes(), baseline.stat().st_mtime_ns), (baseline_bytes, baseline_time))

    def test_ninja_failure_and_timeout_record_exit_before_throw_or_handoff(self):
        for rc in (9, 124):
            with self.subTest(rc=rc):
                self.calls.unlink(missing_ok=True)
                self.env["MOCK_NINJA_RC"] = str(rc)
                result = self.run_prep()
                self.assertEqual(result.returncode == 0, rc == 124, result.stdout + result.stderr)
                expected = ["evidence-before", "ninja", "evidence-after"]
                if rc == 124:
                    expected.append("handoff:Synced")
                else:
                    self.assertIn("ninja failed (exit 9)", result.stderr)
                self.assertEqual(self.phases()[-len(expected):], expected)
                self.assertNotIn("package", self.phases())
                self.assertEqual(json.loads((self.work / "upstream-reuse/result.json").read_text()), {"exit_code": rc})

    def test_evidence_failure_is_fatal_and_preserves_failed_ninja_exit(self):
        for phase, rc in (("before", 0), ("after", 0), ("after", 9), ("after", 124)):
            with self.subTest(phase=phase, rc=rc):
                self.calls.unlink(missing_ok=True)
                self.env.update(MOCK_EVIDENCE_FAILURE=phase, MOCK_NINJA_RC=str(rc))
                result = self.run_prep()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertRegex(result.stderr, rf"evidence collection failed {phase}[\s|]+Ninja \(exit 37\)")
                self.assertEqual(self.phases()[-1], "evidence-" + phase)
                self.assertEqual("ninja" in self.phases(), phase == "after")
                self.assertNotIn("package", self.phases())
                self.assertNotIn("handoff:Synced", self.phases())
                if rc:
                    self.assertIn(f"ninja failed (exit {rc})", result.stderr)

    def test_torque_and_budget_only_paths_never_collect_evidence(self):
        for validate, minutes, last in (("1", "250", "torque"), ("0", "59", "handoff:Synced")):
            with self.subTest(validate=validate, minutes=minutes):
                self.calls.unlink(missing_ok=True)
                self.env.update(MOCK_VALIDATE=validate, MOCK_MINUTES=minutes)
                result = self.run_prep()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.phases()[-1], last)
                self.assertNotIn("ninja", self.phases())
                self.assertFalse(any(phase.startswith("evidence-") for phase in self.phases()))
                self.assertFalse((self.work / "upstream-reuse").exists())

    def test_native_tools_keep_internal_objects_but_recheck_external_sdk_on_resume(self):
        rust = self.src / "third_party/rust-toolchain/bin"
        shutil.copyfile(rust / "cargo.exe", rust / "bindgen.exe")
        self.put(rust / "libclang.dll", "fixture runtime")
        shutil.copyfile(rust / "cargo.exe", self.out / "gn.exe")
        before = (self.out / "obj/retained.obj").stat().st_mtime_ns
        first = self.run_prep()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.phases(), ["verify", "verify", "inspect", "ninja-guard", "finish", "gn-gen", "ninja-plan",
                                         "torque", "evidence-before", "ninja", "evidence-after", "package", "verify-bundle"])
        self.assertEqual((self.out / "obj/retained.obj").stat().st_mtime_ns, before)
        self.assertFalse((self.out / "obj/sdk.obj").exists())
        self.assertEqual(self.report()["dependencies"]["external_dependency_outputs"], 1)
        self.assertEqual(self.report()["removed_final_products"], ["chrome.exe", "chrome.dll", "chrome_elf.dll"])
        self.put(self.out / "obj/sdk.obj", "rebuilt SDK object")
        self.calls.unlink()
        self.env.update(MOCK_STAGE="2", WindowsSDKVersion="changed-fixture-sdk")
        resumed = self.run_prep()
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertFalse((self.out / "obj/sdk.obj").exists())
        self.assertEqual((self.out / "obj/retained.obj").stat().st_mtime_ns, before)
        self.assertEqual(self.report()["counters"]["environment_rechecks"], 1)

    def test_failed_tool_probe_or_bindgen_never_reaches_gn_or_claims_prepared(self):
        for failed_tool in ("clang-cl", "node", "bindgen", "builder"):
            with self.subTest(failed_tool=failed_tool):
                self.env["MOCK_PROBE_FAILURE"] = failed_tool
                self.env["MOCK_BINDGEN_FAILURE"] = "1" if failed_tool == "builder" else "0"
                bindgen = self.src / "third_party/rust-toolchain/bin/bindgen.exe"
                bindgen.unlink(missing_ok=True)
                self.calls.unlink(missing_ok=True)
                result = self.run_prep()
                self.assertNotEqual(result.returncode, 0)
                expected = ["verify", "verify", "inspect", "ninja-guard", "normalize", "bindgen"]
                if failed_tool != "builder":
                    expected.append("finish")
                self.assertEqual(self.phases(), expected)
                self.assertNotIn("gn-bootstrap", self.phases())
                self.assertNotIn("gn-gen", self.phases())
                self.assertNotIn("ninja-plan", self.phases())
                report = self.report()
                self.assertFalse(report["ready_for_gn"])
                self.assertEqual(report["phase"], "inspect" if failed_tool == "builder" else "finish")
                if failed_tool != "builder":
                    self.assertIn(failed_tool, report["error"])
                self.assertFalse((self.src / ".chromix-restored-build-prepared.json").exists())
                self.assertTrue((self.out / "chrome.exe").exists())
                self.assertTrue((self.src / ".chromix-restored-build-inspection.json").exists())


class WindowsArm64PgoTestMixin:
    def test_required_pgo_fails_before_gn_on_initial_build_and_resume(self):
        fixture = getattr(self, 'fixture', self)
        path = fixture.out / 'args.gn'
        for resume in (False, True):
            valid = path.read_text()
            without_pgo = ''.join(line for line in valid.splitlines(keepends=True)
                                  if not line.startswith('chrome_pgo_phase'))
            for assignment in ('', 'chrome_pgo_phase = true\n', 'chrome_pgo_phase = 3\n',
                               'chrome_pgo_phase = "2"\n'):
                with self.subTest(resume=resume, assignment=assignment):
                    path.write_text(without_pgo + assignment)
                    before = path.read_bytes()
                    fixture.calls.unlink(missing_ok=True)
                    failed = fixture.run_prep()
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn('chrome_pgo_phase', failed.stdout + failed.stderr)
                    phases = fixture.phases()
                    self.assertIn('verify', phases)
                    self.assertIn('inspect', phases)
                    self.assertNotIn('gn-gen', phases)
                    self.assertNotIn('ninja', phases)
                    self.assertEqual(path.read_bytes(), before)
            path.write_text(valid)
            fixture.calls.unlink(missing_ok=True)
            passed = fixture.run_prep()
            self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
            fixture.env.update(MOCK_STAGE='2', MOCK_LOCAL_RESUME='1')

    def test_receipt_verification_precedes_required_pgo_validation(self):
        fixture = getattr(self, 'fixture', self)
        receipt_path = fixture.src / '.chromix-upstream-restored.json'
        receipt = json.loads(receipt_path.read_text())
        receipt['valid'] = False
        receipt_path.write_text(json.dumps(receipt))
        path = fixture.out / 'args.gn'
        path.write_text('target_cpu="arm64"\nchrome_pgo_phase = true\n')
        before = path.read_bytes()
        failed = fixture.run_prep()
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(fixture.phases(), ['verify'])
        self.assertNotIn('GN argument merge failed', failed.stderr)
        self.assertEqual(path.read_bytes(), before)


@unittest.skipUnless(shutil.which('pwsh') and shutil.which('patch'), 'PowerShell and GNU patch required')
class WindowsArm64RestoredBuildStageTest(WindowsArm64PgoTestMixin, WindowsRestoredBuildStageTest):
    arch = 'arm64'


@unittest.skipUnless(shutil.which('pwsh') and shutil.which('patch'), 'PowerShell and GNU patch required')
class WindowsArm64LocalBuildTest(WindowsArm64PgoTestMixin, unittest.TestCase):
    def setUp(self):
        fixture = WindowsRestoredBuildStageTest()
        fixture.arch = 'arm64'
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        self.fixture = fixture
        fixture.env.update(MOCK_STAGE='2', MOCK_LOCAL_RESUME='0')
        source = (REPO / 'build/windows/build.ps1').read_text()
        # Native tool execution and final PE version metadata are covered separately.
        source = source[:source.index('\n$chrome = Join-Path $Out')]
        source = source.replace('$Repo = (Resolve-Path "$PSScriptRoot\\..\\..").Path', '$Repo = $env:MOCK_REPO')
        source = source.replace('& $gn gen $Out --fail-on-unused-args',
                                'Invoke-FixtureGn gen $Out --fail-on-unused-args')
        source = source.replace('& $Ninja -C $Out -n chrome', 'Invoke-FixtureNinja $Ninja -C $Out -n chrome')
        source = source.replace('& $Ninja -C $Out -j $Jobs chrome', 'Invoke-FixtureCompile -C $Out -j $Jobs chrome')
        fixture.put(fixture.root / 'build.ps1', source)
        fixture.put(fixture.root / 'assert-arm64-toolchain.ps1', 'param($ChromiumVersion)\n')
        prelude = fixture.wrapper.read_text().split('$domainProgress = Join-Path $Src', 1)[0]
        fixture.put(fixture.wrapper, prelude + r'''
$OutDir = Join-Path $Src "out/Default"
function Invoke-FixtureCompile {
  if ($args.Count -ne 5 -or $args[0] -ne "-C" -or $args[1] -ne $OutDir -or
      $args[2] -ne "-j" -or $args[3] -ne 8 -or $args[4] -ne "chrome") { throw "unexpected compile arguments" }
  Add-Content -LiteralPath $env:MOCK_CALLS -Value "ninja"
  $global:LASTEXITCODE = [int]$env:MOCK_NINJA_RC
}
& (Join-Path $PSScriptRoot "build.ps1") -WorkDir $WorkDir -Arch arm64 -Resume:($env:MOCK_LOCAL_RESUME -eq "1")
''')

    def test_local_pipeline_uses_arm64_receipts_and_x64_host_tools(self):
        fixture = self.fixture
        result = fixture.run_prep()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fixture.phases(), ['verify', 'inspect', 'ninja-guard', 'normalize', 'bindgen',
                                          'finish', 'gn-bootstrap', 'gn-gen', 'ninja-plan',
                                          'evidence-before', 'ninja', 'evidence-after'])
        self.assertEqual(fixture.report()['arch'], 'arm64')
        self.assertTrue(fixture.report()['ready_for_gn'])
        args = (fixture.out / 'args.gn').read_text()
        self.assertIn('target_cpu = "arm64"', args)
        self.assertIn('chrome_pgo_phase = 2 # donor Windows override', args)
        self.assertEqual(args.count('chrome_pgo_phase'), 1)
        self.assertFalse((fixture.src / 'out/Chromix').exists())
        self.assertEqual((fixture.work / '.chromix-target-arch').read_text().strip(), 'arm64')
        fixture.calls.unlink()
        fixture.env.update(MOCK_NINJA_RC='9', MOCK_LOCAL_RESUME='1')
        failed = fixture.run_prep()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('ninja failed (exit 9)', failed.stderr)
        self.assertEqual(fixture.phases()[-3:], ['evidence-before', 'ninja', 'evidence-after'])
        self.assertEqual((fixture.out / 'args.gn').read_text(), args)

    def test_local_pipeline_rejects_x64_receipt_before_preparation(self):
        fixture = self.fixture
        path = fixture.src / '.chromix-upstream-restored.json'
        receipt = json.loads(path.read_text())
        receipt['arch'] = 'x64'
        fixture.put(path, json.dumps(receipt))
        failed = fixture.run_prep()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('receipt platform/architecture mismatch', failed.stderr)
        self.assertFalse(fixture.calls.exists())


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell required")
class WindowsRestoreStageMockTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="windows restore stage ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.src = self.work / "src"
        self.work.mkdir()
        self.calls = self.root / "calls"
        self.env = {**os.environ, "MOCK_ROOT": str(self.root), "MOCK_CALLS": str(self.calls),
                    "MOCK_STAGE": "1", "MOCK_USE": "1", "MOCK_ARTIFACT": "0", "MOCK_VALIDATE": "0",
                    "MOCK_FETCH_RC": "0", "MOCK_RESTORE": "hit", "MOCK_MINUTES": "250",
                    "MOCK_PREPARE_EXHAUSTED": "0", "CHROMIX_USE_UPSTREAM_CACHE": "0",
                    "MOCK_PYTHON": sys.executable}
        (self.root / "tools").mkdir()
        for name in ("verify_patch_stack.py", "apply_restored_patches.py"):
            shutil.copyfile(REPO / "tools" / name, self.root / "tools" / name)
        stage = STAGE.read_text()
        start = stage.index('$domainProgress = Join-Path $Src')
        end = stage.index('\nNew-Item -ItemType Directory -Force -Path $OutDir', start)
        self.script = self.root / "stage.ps1"
        self.script.write_text(r'''
$ErrorActionPreference = "Stop"
$Repo = $env:MOCK_ROOT
$WorkDir = Join-Path $Repo "work"
$Src = Join-Path $WorkDir "src"
$OutDir = Join-Path $Src "out/Chromix"
$RestoredUpstream = $false
$StageIndex = [int]$env:MOCK_STAGE
$Arch = "x64"
$ValidateOnly = $env:MOCK_VALIDATE -eq "1"
$FromArtifact = $env:MOCK_ARTIFACT -eq "1"
$UseUpstreamCache = $env:MOCK_USE -eq "1"
$UpstreamRunId = ""
$UpstreamCacheDir = Join-Path $Repo "cache"
$Revisions = @{ ChromiumVersion = "fixture"; UngoogledCommit = "core" }
function Get-RemainingMin { return [int]$env:MOCK_MINUTES }
function Save-Handoff { param($Mode); Add-Content -LiteralPath $env:MOCK_CALLS -Value "handoff:$Mode" }
function Invoke-Tracked {
  param($File, $ArgList, $Cwd, $TimeoutSec)
  Add-Content -LiteralPath $env:MOCK_CALLS -Value "fetch"
  New-Item -ItemType Directory -Force -Path $UpstreamCacheDir | Out-Null
  Set-Content -LiteralPath (Join-Path $UpstreamCacheDir "result.json") -Value '{"status":"hit"}'
  return [int]$env:MOCK_FETCH_RC
}
function python {
  if ($args -contains "restore") {
    Add-Content -LiteralPath $env:MOCK_CALLS -Value "restore"
    if ($env:MOCK_RESTORE -ne "miss") {
      New-Item -ItemType Directory -Force -Path $Src | Out-Null
      if ($env:MOCK_RESTORE -ne "no-receipt") {
        Set-Content -LiteralPath (Join-Path $Src ".chromix-upstream-restored.json") -Value "valid"
      }
    }
  } elseif ($args -contains "verify") {
    Add-Content -LiteralPath $env:MOCK_CALLS -Value "verify"
    if ((Get-Content (Join-Path $Src ".chromix-upstream-restored.json") -Raw).Trim() -ne "valid") {
      $global:LASTEXITCODE = 1
      return
    }
    if ($args -contains "--cache-dir") { throw "verify must not depend on the cache" }
  } elseif ($args -contains (Join-Path $Repo "tools\verify_patch_stack.py")) {
    Add-Content -LiteralPath $env:MOCK_CALLS -Value "source-check"
    & $env:MOCK_PYTHON @args
    $global:LASTEXITCODE = $LASTEXITCODE
    return
  } else { throw "unexpected Python invocation" }
  $global:LASTEXITCODE = 0
}
''' + stage_budget_source() + next(line for line in stage.splitlines() if line.startswith('$RequireUpstreamCache =')) + '\n' + stage[start:end] + r'''
Set-Content -LiteralPath (Join-Path $Repo "out-dir") -Value $OutDir
''', encoding="utf-8")
        (self.root / "prepare-ungoogled.ps1").write_text(r'''
param($Root, $Repo, $DeadlineEpoch, $ReserveMinutes, $Arch = "x64")
Add-Content -LiteralPath $env:MOCK_CALLS -Value "prepare"
if ($ReserveMinutes -ne $PackReserveMin -or $DeadlineEpoch -ne [DateTimeOffset]::new($Deadline).ToUnixTimeSeconds()) {
  throw "preparation did not receive the stage budget"
}
if ($env:MOCK_PREPARE_EXHAUSTED -eq "1") { throw "PREPARE_BUDGET_EXHAUSTED: fixture" }
$Src = Join-Path $Root "src"
New-Item -ItemType Directory -Force -Path $Src | Out-Null
if (Test-Path (Join-Path $Src ".chromix-upstream-restored.json")) {
  python (Join-Path $Repo "tools/restore_upstream_cache.py") --phase verify --platform windows --arch $Arch --workdir $Root
  if ($LASTEXITCODE -ne 0) { throw "mock prepare receipt verification failed" }
}
$ready = Join-Path $Src ".chromix-source-ready"
if ((Test-Path $ready) -and (Get-Content $ready -Raw).Trim() -ne "fixture|core|windows|patches") {
  throw "prepared source key mismatch"
}
if (-not (Test-Path $ready)) {
  Set-Content -LiteralPath $ready -Value "fixture|core|windows|patches"
}
''', encoding="utf-8")
        (self.root / "update-restored-source.ps1").write_text(r'''
param($Src, $OutDir)
throw "ready snapshots must not invoke legacy migration"
''', encoding="utf-8")

    def run_stage(self, **values):
        return subprocess.run([shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.script)],
                              env={**self.env, **values}, capture_output=True, text=True, timeout=20)

    def logged(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def seed_ready_snapshot(self, substituted=False):
        put = WindowsRestoredPreparationFixture.put
        targets = [f"third_party/blink/renderer/modules/webgpu/{name}.cc"
                   for name in ("gpu_adapter_info", "gpu_adapter")]
        patch = "".join(f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n"
                        "@@ -1,3 +1,3 @@\n // persona policy\n-int version = 1;\n+int version = 2;\n"
                        " // https://google.test\n" for name in targets)
        put(self.root / "patches/series", "patches/current.patch\n")
        put(self.root / "patches/current.patch", patch)
        for name in targets:
            put(self.src / name, "// persona policy\nint version = 2;\n// https://" +
                ("blocked.test\n" if substituted else "google.test\n"))
        put(self.src / ".chromix-source-ready", "fixture|core|windows|patches")
        put(self.src / ".chromix-source-unpacked", "fixture")
        put(self.src / "out/Chromix/obj/retained.obj", "cached object")
        if substituted:
            put(self.work / "tooling/ungoogled-chromium/domain_regex.list", r"google\.test#blocked.test" + "\n")
            put(self.work / "tooling/ungoogled-chromium-windows/domain_substitution.list", "\n".join(targets) + "\n")
            put(self.src / ".chromix-domain-substituted", "core")

    def source_snapshot(self):
        return {path.relative_to(self.src): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.src.rglob("*") if path.is_file()}

    def test_ready_snapshot_verifies_without_source_marker_or_object_writes(self):
        for substituted in (False, True):
            with self.subTest(substituted=substituted):
                self.seed_ready_snapshot(substituted)
                before = self.source_snapshot()
                reports = self.work / "fingerprint-diagnostics"
                count = len(list(reports.glob("resume-source-*.json")))
                for attempt in (1, 2):
                    self.calls.unlink(missing_ok=True)
                    result = self.run_stage(MOCK_STAGE="2", MOCK_ARTIFACT="1", MOCK_USE="0")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(self.logged(), ["prepare", "source-check"])
                    self.assertEqual(self.source_snapshot(), before)
                    receipts = list(reports.glob("resume-source-*.json"))
                    self.assertEqual(len(receipts), count + attempt)
                    self.assertTrue(all(json.loads(path.read_text())["status"] == "verified" for path in receipts))

    def test_stale_ready_snapshot_fails_without_rewrites_or_continuing(self):
        self.seed_ready_snapshot()
        path = self.src / "third_party/blink/renderer/modules/webgpu/gpu_adapter_info.cc"
        path.write_bytes(path.read_bytes().replace(b"version = 2", b"version = 1"))
        before = self.source_snapshot()
        result = self.run_stage(MOCK_STAGE="2", MOCK_ARTIFACT="1", MOCK_USE="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing legacy rewrites", result.stderr)
        self.assertEqual(self.logged(), ["prepare", "source-check"])
        self.assertEqual(self.source_snapshot(), before)
        self.assertFalse((self.root / "out-dir").exists())
        report, = (self.work / "fingerprint-diagnostics").glob("resume-source-*.json")
        self.assertEqual(json.loads(report.read_text())["status"], "failed")

    def test_cross_version_snapshot_keeps_source_and_objects_before_rejection(self):
        for marker in ("unpacked", "ready"):
            with self.subTest(marker=marker):
                self.seed_ready_snapshot()
                if marker == "unpacked":
                    (self.src / ".chromix-source-unpacked").write_text("old-version")
                else:
                    (self.src / ".chromix-source-unpacked").unlink()
                    (self.src / ".chromix-source-ready").write_text("old-version|core|windows|patches")
                before = self.source_snapshot()
                result = self.run_stage(MOCK_STAGE="2", MOCK_ARTIFACT="1", MOCK_USE="0")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cross-version snapshot", result.stderr)
                self.assertEqual(self.logged(), [])
                self.assertEqual(self.source_snapshot(), before)
                self.assertFalse((self.root / "out-dir").exists())

    def test_fresh_hit_then_stage_two_uses_default_and_revalidates_without_fetch(self):
        first = self.run_stage()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.logged(), ["fetch", "restore", "verify", "prepare", "verify"])
        self.assertTrue((self.root / "out-dir").read_text().strip().replace('\\', '/').endswith('/src/out/Default'))
        self.assertFalse((self.work / "upstream-cache-preparation.json").exists())
        self.calls.unlink()
        resumed = self.run_stage(MOCK_STAGE="2", MOCK_ARTIFACT="1")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(self.logged(), ["verify", "prepare", "verify"])
        self.assertTrue((self.root / "out-dir").read_text().strip().replace('\\', '/').endswith('/src/out/Default'))

    def test_miss_and_timeout_fail_without_preparation_or_chromix_out(self):
        missed = self.run_stage(MOCK_RESTORE="miss")
        self.assertNotEqual(missed.returncode, 0)
        self.assertEqual(self.logged(), ["fetch", "restore"])
        self.assertFalse(self.src.exists())
        self.assertFalse((self.root / "out-dir").exists())
        self.calls.unlink()
        timed = self.run_stage(MOCK_FETCH_RC="124")
        self.assertNotEqual(timed.returncode, 0)
        self.assertEqual(self.logged(), ["fetch"])

    def test_no_opt_in_allows_validation_and_existing_cold_source(self):
        for values in ({"MOCK_VALIDATE": "1"}, {}):
            with self.subTest(values=values):
                result = self.run_stage(MOCK_USE="0", **values)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.logged(), ["prepare"])
                self.calls.unlink()

    def test_unreceipted_restore_and_corrupt_resume_fail_before_preparation(self):
        failed = self.run_stage(MOCK_RESTORE="no-receipt")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.logged(), ["fetch", "restore"])
        (self.src / ".chromix-upstream-restored.json").write_text("invalid")
        self.calls.unlink()
        failed = self.run_stage(MOCK_STAGE="2", MOCK_ARTIFACT="1")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.logged(), ["verify"])

    def test_validation_preparation_requires_margin_and_never_hands_off(self):
        for minutes, exhausted, calls, succeeds in ((44, "0", [], False), (45, "0", ["prepare"], True),
                                                   (60, "1", ["prepare"], False)):
            with self.subTest(minutes=minutes, exhausted=exhausted):
                result = self.run_stage(MOCK_VALIDATE="1", MOCK_USE="0", MOCK_MINUTES=str(minutes),
                                        MOCK_PREPARE_EXHAUSTED=exhausted)
                self.assertEqual(result.returncode == 0, succeeds, result.stdout + result.stderr)
                self.assertEqual(self.logged(), calls)
                if self.calls.exists():
                    self.calls.unlink()

    def test_stage_budget_and_ready_mismatch_cannot_bypass_preparation_checks(self):
        low = self.run_stage(MOCK_MINUTES="60")
        self.assertNotEqual(low.returncode, 0)
        self.assertEqual(self.logged(), [])
        low = self.run_stage(MOCK_MINUTES="60", MOCK_USE="0")
        self.assertEqual(low.returncode, 0, low.stdout + low.stderr)
        self.assertEqual(self.logged(), ["handoff:Unsynced"])
        self.calls.unlink()
        self.src.mkdir()
        (self.src / ".chromix-source-ready").write_text("fixture|wrong-pins")
        failed = self.run_stage(MOCK_STAGE="2", MOCK_ARTIFACT="1")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.logged(), [])
        failed = self.run_stage(MOCK_STAGE="2", MOCK_ARTIFACT="1", MOCK_USE="0")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.logged(), ["prepare"])


if __name__ == "__main__":
    unittest.main()
