"""POSIX cross-run checkpoints must migrate before normal build verification."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

REPO = Path(__file__).resolve().parents[2]


def workflow(name):
    return yaml.safe_load((REPO / '.github/workflows' / name).read_text())


class PosixCheckpointWorkflowTest(unittest.TestCase):
    def test_supported_entries_expose_checkpoint_selection(self):
        for platform in ('macos', 'linux'):
            for arch in ('x64', 'arm64'):
                entry = workflow(f'build-{platform}-{arch}.yml')
                events = entry.get('on', entry.get(True))
                inputs = events['workflow_dispatch']['inputs']
                for field, default in (('resume_run_id', ''), ('resume_tree_stage', '7'), ('resume_attempt', '1'), ('resume_artifact_ids', '')):
                    self.assertEqual(inputs[field]['type'], 'string')
                    self.assertEqual(inputs[field]['default'], default)
                    self.assertIn(f'inputs.{field}', entry['jobs']['build']['with'][field])

    def test_donor_validation_precedes_exact_checkout_download_and_migration(self):
        stages = workflow('build-posix-github.yml')['jobs']
        steps = stages['posix-1']['steps']
        names = {step.get('name'): step for step in steps}
        ordered = ['Validate selected POSIX checkpoint', 'Check out checkpoint patch definitions',
                   'Download selected POSIX checkpoint', 'Restore and migrate selected POSIX checkpoint', 'Run stage 1']
        self.assertEqual(sorted(ordered, key=lambda name: steps.index(names[name])), ordered)
        for name in ordered[:-1]:
            self.assertEqual(names[name]['if'], "inputs.resume_run_id != ''")
        checkout = names[ordered[1]]['with']
        self.assertEqual(checkout['ref'], '${{ steps.resume.outputs.head_sha }}')
        self.assertFalse(checkout['persist-credentials'])
        download = names[ordered[2]]
        self.assertNotIn('uses', download)
        self.assertEqual(download['env']['GH_TOKEN'], '${{ github.token }}')
        self.assertIn('tools/download_posix_snapshot.py', download['run'])
        self.assertIn('--manifest "${RUNNER_TEMP}/chromix-logs/snapshot-origin.json"', download['run'])
        self.assertIn('--destination "${RUNNER_TEMP}/chromix-restore"', download['run'])
        self.assertIn('--report "${RUNNER_TEMP}/chromix-logs/snapshot-download.json"', download['run'])
        validate = names[ordered[0]]
        self.assertEqual(validate['env']['GH_TOKEN'], '${{ github.token }}')
        self.assertNotIn('UPSTREAM_ACTIONS_TOKEN', str(validate) + str(download))
        self.assertEqual(validate['env']['SNAPSHOT_ATTEMPT'], '${{ inputs.resume_attempt }}')
        self.assertIn('--platform "$BUILD_PLATFORM"', validate['run'])
        self.assertNotIn('test "$BUILD_PLATFORM" = macos', validate['run'])
        self.assertIn('test "$CACHE_REQUIRED" = true', validate['run'])
        migrate = names[ordered[3]]['run']
        self.assertIn('set -euo pipefail', migrate)
        self.assertIn('bash build/posix/restore-snapshot.sh "$RESTORE" "$WORK"', migrate)
        self.assertLess(migrate.index('restore-snapshot.sh'), migrate.index('migrate_restored_snapshot.py'))
        self.assertNotIn('.chromix-previous-repo/tools/', migrate)
        for number in range(2, 9):
            self.assertNotIn('migrate_restored_snapshot.py', str(stages[f'posix-{number}']))
        self.assertNotIn('needs', stages['posix-1'])

    def test_failed_terminal_stage_can_upload_only_verified_checkpoint(self):
        for number in range(1, 9):
            stage = workflow('build-posix-github.yml')['jobs'][f'posix-{number}']
            steps = stage['steps']
            build = next(step for step in steps if step.get('id') == 'stage')
            self.assertNotIn('continue-on-error', build)
            verify = next(step for step in steps if step.get('id') == 'checkpoint')
            self.assertIn('set -euo pipefail', verify['run'])
            self.assertIn('python3 tools/snapshot_volumes.py "$SNAP"', verify['run'])
            self.assertIn('zstd -d -T0 | tar -tf -', verify['run'])
            self.assertIn('!cancelled()', verify['if'])
            self.assertIn("steps.stage.outputs.upload_snapshot == 'true'", verify['if'])
            self.assertNotIn('success()', verify['if'])
            uploads = [step for step in steps if step.get('name', '').startswith('Upload tree part')]
            self.assertEqual(len(uploads), 4)
            for upload in uploads:
                self.assertIn('!cancelled()', upload['if'])
                self.assertIn("steps.checkpoint.outcome == 'success'", upload['if'])
                self.assertNotIn('success()', upload['if'])
            final = next(step for step in steps if step.get('name') == 'Upload final bundle')
            self.assertEqual(final['if'], "steps.stage.outputs.finished == 'true'")

    def test_failed_mac_runtime_bundle_is_diagnostic_only(self):
        for number in range(1, 9):
            steps = workflow('build-posix-github.yml')['jobs'][f'posix-{number}']['steps']
            failed = next(step for step in steps if step.get('name') == 'Upload failed macOS runtime bundle')
            for guard in ('!cancelled()', "inputs.platform == 'macos'",
                          "steps.stage.outputs.package_ready == 'true'",
                          "steps.stage.outcome == 'failure'",
                          "steps.stage.outputs.runtime_failed == 'true'"):
                self.assertIn(guard, failed['if'])
            self.assertIn(f'-failed-runtime-s{number}-attempt-', failed['with']['name'])
            self.assertEqual(failed['with']['if-no-files-found'], 'error')
            for path in (f'/runtime-smoke-stage-{number}/', '/fingerprint-diagnostics/',
                         f'/chromix-logs/stage-{number}.log', '/dist/SHA256SUMS',
                         '/dist/${{ inputs.artifact }}.zip'):
                self.assertIn(path, failed['with']['path'])
            snapshot = next(step for step in steps if step.get('id') == 'runtime_checkpoint')
            self.assertEqual(snapshot['if'], failed['if'])
            self.assertLess(steps.index(failed), steps.index(snapshot))
            verify = next(step for step in steps if step.get('id') == 'checkpoint')
            self.assertLess(steps.index(snapshot), steps.index(verify))
            for step in [verify, *[item for item in steps if item.get('name', '').startswith('Upload tree part')]]:
                self.assertIn("steps.runtime_checkpoint.outputs.upload_snapshot == 'true'", step['if'])
            logs = next(step for step in steps if step.get('name') == 'Upload build diagnostics')
            self.assertEqual(logs['if'], 'always()')
            self.assertIn(f'/runtime-smoke-stage-{number}/', logs['with']['path'])
            final = next(step for step in steps if step.get('name') == 'Upload final bundle')
            self.assertEqual(final['if'], "steps.stage.outputs.finished == 'true'")

    def test_fingerprint_receipts_and_native_gate_survive_runtime_recovery(self):
        jobs = workflow('build-posix-github.yml')['jobs']
        for number in range(1, 9):
            stage = jobs[f'posix-{number}']
            steps = stage['steps']
            receipt = next(step for step in steps if step.get('name') == 'Upload fingerprint source receipt')
            self.assertEqual(receipt['if'], "steps.stage.outputs.finished == 'true'")
            self.assertEqual(receipt['with']['name'], '${{ inputs.artifact }}-fingerprint-source')
            self.assertTrue(receipt['with']['path'].endswith('/fingerprint-diagnostics/source-final.json'))
            self.assertEqual(receipt['with']['if-no-files-found'], 'error')
            logs = next(step for step in steps if step.get('name') == 'Upload build diagnostics')
            self.assertIn('/fingerprint-diagnostics/', logs['with']['path'])
            if number > 1:
                self.assertIn(f"needs.posix-{number - 1}.result == 'success'", stage['if'])
        native = jobs['verify-linux-arm64']
        self.assertEqual(native['timeout-minutes'], 50)
        steps = native['steps']
        receipt = next(step for step in steps if step.get('name') == 'Download same-run source verification receipt')
        self.assertEqual(receipt['with']['name'], '${{ inputs.artifact }}-fingerprint-source')
        gate = next(step for step in steps if step.get('name') == 'Native ARM64 fingerprint regression gate')
        self.assertLess(steps.index(receipt), steps.index(gate))
        self.assertIn('timeout -k 30s 2100s python3 tools/fingerprint_acceptance.py', gate['run'])
        self.assertIn('--source-report "${RUNNER_TEMP}/chromix-native-source/source-final.json"', gate['run'])
        self.assertNotIn('continue-on-error', gate)
        logs = next(step for step in steps if step.get('name') == 'Upload native verification diagnostics')
        self.assertEqual(logs['if'], 'always()')
        self.assertIn('/chromix-native-fingerprint/', logs['with']['path'])

    def test_failed_runtime_snapshot_emits_only_after_success(self):
        steps = workflow('build-posix-github.yml')['jobs']['posix-1']['steps']
        script = next(step['run'] for step in steps if step.get('id') == 'runtime_checkpoint')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            helper = root / 'build/posix/ci-parts.sh'
            helper.parent.mkdir(parents=True)
            for code in (0, 23):
                helper.write_text(f'#!/bin/sh\nexit {code}\n')
                output = root / 'output'
                output.write_text('')
                result = subprocess.run(['bash', '-c', script], cwd=root, capture_output=True,
                                        text=True, env=dict(os.environ, RUNNER_TEMP=temp,
                                                           GITHUB_OUTPUT=str(output)), timeout=5)
                self.assertEqual(result.returncode, code)
                self.assertEqual(output.read_text(), '' if code else 'upload_snapshot=true\n')

    def test_sdk_preflight_precedes_large_downloads_on_every_mac_stage(self):
        for number in range(1, 9):
            steps = workflow('build-posix-github.yml')['jobs'][f'posix-{number}']['steps']
            inspect = next(step for step in steps if step.get('name') == 'Verify complete Mac SDK contents')
            self.assertEqual(inspect['if'], "runner.os == 'macOS' && inputs.use_upstream_cache")
            self.assertIn('inspect_macos_sdk.py', inspect['run'])
            for step in steps:
                if step.get('uses') == 'actions/download-artifact@v4' or step.get('name') == 'Download selected POSIX checkpoint':
                    self.assertLess(steps.index(inspect), steps.index(step))

    def test_build_only_manual_repairs_do_not_trigger_automatic_release(self):
        release = workflow('release-browser.yml')
        guard = release['jobs']['readiness']['if']
        self.assertIn("github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'", guard)
        self.assertIn("!(github.event.workflow_run.event == 'workflow_dispatch' &&", guard)
        self.assertIn("contains(github.event.workflow_run.head_commit.message, '[skip ci]')", guard)
        self.assertEqual(release['permissions']['contents'], 'read')
        self.assertEqual(release['jobs']['release']['if'], "needs.readiness.outputs.ready == 'true'")

    def test_truncated_zstd_fails_before_creating_work_tree(self):
        if not shutil.which('zstd'):
            self.skipTest('zstd unavailable')
        steps = workflow('build-posix-github.yml')['jobs']['posix-1']['steps']
        script = next(step['run'] for step in steps if step.get('name') == 'Restore and migrate selected POSIX checkpoint')
        script = script.split('python3 tools/migrate_restored_snapshot.py')[0]
        compressed = subprocess.check_output(['zstd', '-q', '-c'], input=b'restore fixture' * 1024)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            restore = root / 'chromix-restore'
            restore.mkdir()
            volume = restore / 'tree.tar.zst.001'
            volume.write_bytes(compressed[:-5])
            result = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                                    env=dict(os.environ, RUNNER_TEMP=temp))
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'chromix-build').exists())
            self.assertEqual(volume.read_bytes(), compressed[:-5])

    def test_resume_shell_parses_under_bash32(self):
        shell = Path.home() / '.local/bash-3.2-for-ci/bash'
        if not shell.exists():
            self.skipTest('bash 3.2 unavailable')
        steps = workflow('build-posix-github.yml')['jobs']['posix-1']['steps']
        with tempfile.TemporaryDirectory() as temp:
            for index, step in enumerate(steps):
                if 'run' not in step or 'checkpoint' not in step.get('name', '').lower():
                    continue
                script = Path(temp) / f'{index}.sh'
                script.write_text(step['run'])
                result = subprocess.run([str(shell), '-n', str(script)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
