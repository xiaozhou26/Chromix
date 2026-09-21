"""Windows ARM64 stages remain isolated and require native acceptance."""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import gen_windows_arm64_workflow as generator


class WindowsArm64WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.workflow = generator.workflow()

    def test_generated_workflow_is_current(self):
        self.assertEqual(generator.OUTPUT.read_text(), generator.render())
        self.assertEqual(yaml.safe_load(generator.render()), self.workflow)

    def test_separate_entry_and_cache_policy(self):
        workflow = self.workflow
        self.assertEqual(workflow['name'], 'build-win-arm64-github')
        self.assertEqual(workflow['concurrency']['group'], 'build-win-arm64-github-${{ github.ref }}')
        self.assertFalse(workflow['concurrency']['cancel-in-progress'])
        for event in ('workflow_dispatch', 'workflow_call'):
            self.assertEqual(set(workflow['on'][event]['inputs']),
                             {'build_profile', 'compile_jobs', 'use_upstream_cache', 'upstream_run_id'})
            self.assertIs(workflow['on'][event]['inputs']['use_upstream_cache']['default'], False)
        self.assertEqual(workflow['on']['push']['branches'], ['main'])
        self.assertEqual(workflow['env']['CHROMIX_USE_UPSTREAM_CACHE'],
                         "${{ (inputs.use_upstream_cache || inputs.upstream_run_id != '') && '1' || '0' }}")
        self.assertEqual(workflow['env']['CHROMIX_PREFER_UPSTREAM_CACHE'], '0')
        self.assertEqual(workflow['env']['CHROMIX_TARGET_ARCH'], 'arm64')
        self.assertNotIn('resume_run_id', generator.render())
        self.assertNotIn('CHROMIX_WINDOWS_MIGRATION_PROFILE', workflow['env'])
        self.assertEqual(workflow['on']['workflow_call']['secrets'],
                         {'UPSTREAM_ACTIONS_TOKEN': {'required': False}})
        references = set(re.findall(r'inputs\.([a-z_]+)', generator.render()))
        self.assertEqual(references, set(workflow['on']['workflow_dispatch']['inputs']))

    def test_twelve_stages_keep_snapshot_safety_and_target_names(self):
        for index in range(1, 13):
            job = self.workflow['jobs'][f'build-{index}']
            with self.subTest(stage=index):
                self.assertEqual(job['runs-on'], 'windows-2022')
                self.assertEqual(job['timeout-minutes'], 355)
                steps = job['steps']
                stage = next(step for step in steps if step.get('id') == 'stage')
                if index == 1:
                    self.assertIn('StageIndex = 1', stage['run'])
                    self.assertIn('MaxStages = 12', stage['run'])
                    self.assertIn("UseUpstreamCache = ($env:USE_UPSTREAM_CACHE -eq 'true')", stage['run'])
                    self.assertIn('$stageArgs.UpstreamRunId = $env:UPSTREAM_RUN_ID', stage['run'])
                    self.assertEqual(stage['env'], {
                        'USE_UPSTREAM_CACHE': '${{ inputs.use_upstream_cache }}',
                        'UPSTREAM_RUN_ID': '${{ inputs.upstream_run_id }}',
                        'GH_TOKEN': '${{ secrets.UPSTREAM_ACTIONS_TOKEN || github.token }}',
                    })
                else:
                    self.assertIn(f'-StageIndex {index} -MaxStages 12', stage['run'])
                    self.assertNotIn('env', stage)
                self.assertNotIn('${{', stage['run'])
                self.assertNotIn('continue-on-error', stage)
                selection = next(step for step in steps if step.get('name') == 'Select compile parallelism')
                self.assertLess(steps.index(selection), steps.index(stage))
                self.assertIn('tools/build_resources.py --github-env', selection['run'])
                if index > 1:
                    previous = f'build-{index - 1}'
                    self.assertEqual(job['needs'], previous)
                    self.assertIn(f"needs.{previous}.result == 'success'", job['if'])
                    download = next(step for step in steps if step.get('uses') == 'actions/download-artifact@v4')
                    self.assertEqual(download['with']['pattern'],
                        f'win-arm64-tree-s{index-1}-attempt-${{{{ needs.{previous}.outputs.snapshot_attempt }}}}-part*')
                    self.assertNotIn('run-id', download['with'])
                    self.assertIn('-FromArtifact', stage['run'])
                snapshots = [step for step in steps if step.get('name', '').startswith('Upload tree part')]
                self.assertEqual(len(snapshots), 4)
                for part, step in enumerate(snapshots, 1):
                    self.assertIn("steps.stage.outputs.snapshot_safe == 'true'", step['if'])
                    self.assertIn("steps.stage.outputs.finished != 'true'", step['if'])
                    self.assertEqual(step['with']['name'],
                                     f'win-arm64-tree-s{index}-attempt-${{{{ github.run_attempt }}}}-part{part}')
                final = next(step for step in steps if step.get('name') == 'Upload final bundle')
                self.assertEqual(final['with']['name'], 'win-arm64')
                self.assertIn('chromix-win-arm64.zip', final['with']['path'])
                self.assertEqual(final['with']['if-no-files-found'], 'error')
                receipt = next(step for step in steps if step.get('name') == 'Upload final source receipt')
                self.assertEqual(receipt['if'], final['if'])
                self.assertEqual(receipt['with']['path'], 'C:\\c\\dist\\source-verification.json')
                self.assertEqual(receipt['with']['if-no-files-found'], 'error')

    def test_cache_diagnostics_survive_failures_and_use_target_artifact_names(self):
        for index in range(1, 13):
            steps = self.workflow['jobs'][f'build-{index}']['steps']
            name = 'Upload upstream cache diagnostics' if index == 1 else 'Upload restored reuse evidence'
            upload = next(step for step in steps if step.get('name') == name)
            self.assertEqual(upload['if'], '${{ always() }}')
            self.assertEqual(upload['with']['if-no-files-found'], 'ignore')
            self.assertIn('win-arm64', upload['with']['name'])
            self.assertIn('${{ github.run_attempt }}', upload['with']['name'])
            for path in ('upstream-reuse\\baseline.json', 'upstream-reuse\\result.json'):
                self.assertIn(path, upload['with']['path'])
            if index == 1:
                self.assertTrue(upload['with']['include-hidden-files'])
                for path in ('C:\\u\\result.json', '.chromix-upstream-restored.json',
                             'upstream-cache-restore.json', 'upstream-cache-preparation.json'):
                    self.assertIn(path, upload['with']['path'])

    def test_workflow_powershell_commands_parse(self):
        powershell = shutil.which('pwsh') or '/opt/pwsh/pwsh'
        if not Path(powershell).is_file():
            self.skipTest('PowerShell is unavailable')
        commands = [step['run'] for job in self.workflow['jobs'].values() for step in job['steps']
                    if step.get('shell') in ('powershell', 'pwsh') and 'run' in step]
        with tempfile.TemporaryDirectory() as temp:
            for index, command in enumerate(commands):
                Path(temp, f'{index}.ps1').write_text(command)
            result = subprocess.run([powershell, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command',
                '$ErrorActionPreference = "Stop"; Get-ChildItem -LiteralPath "' + temp + '" | ForEach-Object { '
                '$tokens = $null; $errors = $null; '
                '[Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$tokens, [ref]$errors) | Out-Null; '
                'if ($errors.Count) { throw ($errors | Out-String) } }'],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_release_and_sdk_names_match_the_native_job(self):
        import release_browser
        self.assertEqual(release_browser.WORKFLOWS[self.workflow['name']], ('chromix-win-arm64',))
        self.assertEqual(release_browser.ARTIFACT_NAMES['chromix-win-arm64'], 'win-arm64')
        self.assertEqual(release_browser.ARM64_NATIVE_JOB, self.workflow['jobs']['verify-arm64']['name'])
        for path in ('sdk/node/_binary.js', 'sdk/python/chromix/_binary.py'):
            source = (ROOT / path).read_text()
            self.assertIn('chromix-win-arm64.zip', source)
            self.assertIn('win-arm64', source)
        release = yaml.safe_load((ROOT / '.github/workflows/release-browser.yml').read_text())
        events = release.get('on', release.get(True))
        self.assertIn(self.workflow['name'], events['workflow_run']['workflows'])

    def test_source_receipt_is_exported_only_after_bundle_verification(self):
        stage = (ROOT / 'build/windows/ci-stage.ps1').read_text()
        final = stage[stage.index('if ($rc -eq 0) {'):]
        self.assertLess(final.index('Verify-FinalBundle'), final.index('dist\\source-verification.json'))
        self.assertLess(final.index('dist\\source-verification.json'), final.index('Write-OutVar finished true'))
        self.assertIn('Copy-Item -LiteralPath $FingerprintSourceReport', final)
        self.assertIn('if ($Arch -eq "arm64")', final)

    def test_native_verification_is_required_and_uses_same_run(self):
        job = self.workflow['jobs']['verify-arm64']
        self.assertEqual(job['runs-on'], 'windows-11-arm')
        self.assertEqual(job['needs'], 'complete')
        self.assertIn("needs.complete.result == 'success'", job['if'])
        self.assertNotIn('continue-on-error', job)
        downloads = [step for step in job['steps'] if step.get('uses') == 'actions/download-artifact@v4']
        self.assertEqual({step['with']['name'] for step in downloads},
                         {'win-arm64', 'chromix-win-arm64-source-receipt'})
        for step in downloads:
            self.assertNotIn('run-id', step['with'])
        smoke = next(step for step in job['steps'] if 'headless exit' in step.get('name', ''))
        self.assertIn('--arch arm64 --native', smoke['run'])
        self.assertIn('--sha256-file dist/SHA256SUMS', smoke['run'])
        self.assertIn('if ($LASTEXITCODE -ne 0)', smoke['run'])
        fingerprint = next(step for step in job['steps'] if step.get('name') == 'Verify native ARM64 fingerprint behavior')
        self.assertIn('--source-report source-receipt/source-verification.json', fingerprint['run'])
        self.assertIn('--expected-sha256 $hash', fingerprint['run'])
        self.assertNotIn('--control', fingerprint['run'])
        self.assertIn('if ($LASTEXITCODE -ne 0)', fingerprint['run'])
        for step in job['steps']:
            self.assertNotIn('continue-on-error', step)


if __name__ == '__main__':
    unittest.main()
