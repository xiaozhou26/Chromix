"""Exact compiled stage3 receipts and tool-only same-browser admission."""
import copy
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from tools import validate_macos156_arm64_stage8_checkpoint as arm
from tools import validate_macos156_arm64_stage3_checkpoint as latest
from tools.tests import test_macos156_arm64_stage8_resume as arm_tests
from tools.tests.test_macos156_handoff import emit_source

from tools.tests.macos156_historical import recipe_repo

ROOT = Path(__file__).resolve().parents[2]
HISTORICAL = recipe_repo()
FIXTURE = json.loads(gzip.decompress(
    (ROOT / 'tools/tests/fixtures/macos156_arm64_stage3_receipts.json.gz').read_bytes()))


class Client:
    def __init__(self):
        self.data = copy.deepcopy(FIXTURE)

    def get(self, path):
        return {f'/actions/runs/{latest.RUN}': self.data['run'],
                f'/actions/jobs/{latest.JOB}': self.data['job']}[path]

    def items(self, path, key):
        return self.data['artifacts']


def downloaded():
    return dict(status='success', phase='complete', publication='published', repository=arm.REPOSITORY,
                run_id=latest.RUN, head_sha=latest.SHA, total_size_in_bytes=sum(latest.VOLUME_SIZES),
                artifacts=[dict(a, attempts=[dict(status='success', size_in_bytes=a['size_in_bytes'],
                                                sha256=a['digest'].split(':')[1])]) for a in latest.ARTIFACTS],
                volumes=[dict(name=f'tree.tar.zst.{i:03d}', artifact_id=a['id'], size_in_bytes=size,
                              sha256='a' * 64) for i, (a, size) in
                         enumerate(zip(latest.ARTIFACTS, latest.VOLUME_SIZES), 1)])


class LatestAdmissionTest(unittest.TestCase):
    def test_compiled_stage3_admission_pack_restore_and_attempt_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            work, donor = base / 'work', base / 'donor'
            subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout', str(ROOT), str(donor)], check=True)
            subprocess.run(['git', '-C', str(donor), 'checkout', '--quiet', latest.SHA], check=True)
            for name, text in FIXTURE['files'].items():
                path = work / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            (work / 'src/out/Default').mkdir(parents=True)
            source = work / 'fingerprint-diagnostics/source-final.json'
            source.write_bytes((work / 'fingerprint-diagnostics/mac156-arm64-stage8-continuation-s1-after-attempt-1-source.json').read_bytes())
            origin, report = base / 'origin', base / 'download'
            origin.write_text(json.dumps(latest.expected_manifest()))
            report.write_text(json.dumps(downloaded()))
            _, after = latest.donor_identity(work)
            state = after['state']
            with patch.dict(os.environ, arm_tests.ENV), patch.object(arm, 'source_state', return_value=state), \
                    patch.object(arm, 'native_state'), patch.object(latest, 'compiled_browser'), \
                    patch.object(arm, 'live_source', side_effect=emit_source), \
                    patch.object(arm.migration.restore, 'verify_restored'), patch.object(arm.migration, '_verify_tooling'):
                bad_state = dict(state, **{'out/Default/.ninja_log': 'wrong'})
                with patch.object(arm, 'source_state', return_value=bad_state), self.assertRaises(ValueError):
                    latest.admit(work, donor, HISTORICAL, origin, report)
                result = latest.admit(work, donor, HISTORICAL, origin, report)
                self.assertEqual(result['state'], state)
                self.assertEqual(result['provenance']['manifest'], latest.expected_manifest())
                self.assertEqual(result['changed_files'], [])
                self.assertEqual(result['tool_only_changes'], {})
                with self.assertRaises(ValueError):
                    latest.admit(work, donor, HISTORICAL, origin, report)
                latest.continuation(HISTORICAL, work, 1, 'before')
                with patch.object(arm.restore_ninja, 'host_identity', return_value=('macos', 'arm64')), \
                        patch.object(arm.restore_ninja, 'binary_architectures', return_value={'arm64'}), \
                        patch.object(arm.restore_ninja, 'probe_version', return_value='1.13.2'), \
                        patch.object(arm, 'frozen_ninja', return_value=124) as frozen:
                    self.assertEqual(latest.main(['--mode', 'compile', '--repo', str(HISTORICAL),
                                                  '--workdir', str(work), '--stage', '1']), 124)
                    frozen.assert_called_once()
                latest.continuation(HISTORICAL, work, 1, 'after')
                parts = base / 'parts'
                subprocess.run(['/bin/bash', str(ROOT / 'build/posix/ci-parts.sh'), str(work), str(parts)],
                               check=True, capture_output=True, env=dict(os.environ, COPYFILE_DISABLE='1'))
                restored = base / 'restored'
                subprocess.run([sys.executable, str(ROOT / 'tools/restore_posix_snapshot.py'), str(parts), str(restored)],
                               check=True, capture_output=True)
                with patch.dict(os.environ, GITHUB_RUN_ATTEMPT='2', CHROMIX_PREVIOUS_ATTEMPT='1'):
                    before = latest.continuation(HISTORICAL, restored, 2, 'before')
                    self.assertEqual(before['logical_stage'], 14)
                    latest.continuation(HISTORICAL, restored, 2, 'after')
                latest.donor_identity(restored)
                for name in latest.DONOR_RECEIPTS:
                    self.assertEqual((work / name).read_bytes(), (restored / name).read_bytes())
                wrong = json.loads((restored / latest.RECEIPT).read_bytes())
                for key, value in dict(arch='x64', run_id=arm.RUN, job_id=1, head_sha=arm.SHA,
                                       version='155', preparation_key='other', continuation_run='1').items():
                    (restored / latest.RECEIPT).write_text(json.dumps(dict(wrong, **{key: value})))
                    with self.assertRaises(ValueError):
                        latest.continuation(HISTORICAL, restored, 3, 'before')


class SameBrowserTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.donor = self.base / 'donor'
        subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout', str(ROOT), str(self.donor)], check=True)
        subprocess.run(['git', '-C', str(self.donor), 'checkout', '--quiet', latest.SHA], check=True)
        self.receipt = json.loads(FIXTURE['files'][latest.previous.RECEIPT])

    def test_exact_reviewed_probe_revision_only_and_no_old_helper_relaxation(self):
        name = 'tools/fingerprint_backend_probe.js'
        original = arm.read
        changed = original(HISTORICAL, name) + b'\n// reviewed test revision\n'
        digest = arm.sha(changed)
        def read(root, path):
            return changed if root == HISTORICAL and path == name else original(root, path)
        baseline = arm.recipes(HISTORICAL)
        with patch.object(arm, 'read', side_effect=read):
            with self.assertRaisesRegex(ValueError, 'unreviewed'):
                latest.samebrowser_recipes(HISTORICAL, self.donor, self.receipt)
            with patch.dict(latest.APPROVED_TOOL_REVISIONS, {name: (digest,)}):
                result = latest.samebrowser_recipes(HISTORICAL, self.donor, self.receipt)
                self.assertEqual(result[name], digest)
                self.assertEqual(arm.recipes(HISTORICAL), baseline)
        self.assertEqual(latest.APPROVED_TOOL_REVISIONS, {})

    def test_non_tool_recipe_drift_cannot_be_approved(self):
        original = arm.read
        for name in ('build/args.macos.gn', 'build/macos/build.sh', 'build/macos/package-macos.sh',
                     'tools/restore_ninja.py', 'tools/macos_runtime.py', 'tools/verify_patch_stack.py'):
            changed = original(HISTORICAL, name) + b'\n'
            def read(root, path):
                return changed if root == HISTORICAL and path == name else original(root, path)
            with self.subTest(name=name), patch.object(arm, 'read', side_effect=read), \
                    patch.dict(latest.APPROVED_TOOL_REVISIONS, {name: (arm.sha(changed),)}), \
                    self.assertRaisesRegex(ValueError, 'source/build recipe changed'):
                latest.samebrowser_recipes(HISTORICAL, self.donor, self.receipt)

    def test_every_pinned_donor_receipt_and_failure_remain_immutable(self):
        work = self.base / 'work'
        for name, text in FIXTURE['files'].items():
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        latest.donor_identity(work)
        for name in latest.DONOR_RECEIPTS:
            target = work / name
            raw = target.read_bytes()
            target.write_bytes(raw + b' ')
            with self.subTest(name=name), self.assertRaises(ValueError):
                latest.donor_identity(work)
            target.write_bytes(raw)
        duplicate = work / latest.HANDOFF.replace('attempt-1', 'attempt-2')
        duplicate.write_bytes((work / latest.HANDOFF).read_bytes())
        with self.assertRaisesRegex(ValueError, 'exact single donor'):
            latest.donor_identity(work)

    def test_compiled_browser_rejects_missing_or_changed_output(self):
        work = self.base / 'work'
        target = work / latest.FAILURE
        target.parent.mkdir(parents=True)
        target.write_text(FIXTURE['files'][latest.FAILURE])
        with self.assertRaisesRegex(ValueError, 'compiled donor browser'):
            latest.compiled_browser(ROOT, work)
        binary = work / 'src/out/Default/Chromium.app/Contents/MacOS/Chromium'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'not the compiled donor')
        binary.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'compiled donor browser'):
            latest.compiled_browser(ROOT, work)

    def test_metadata_rejects_missing_failure_preservation_or_log_tampering(self):
        for name in ('Run stage 3', 'Preserve latest macOS compiled checkpoint',
                     'Upload failed macOS runtime bundle', 'Verify handoff snapshot', 'Upload tree part 2'):
            client = Client()
            next(s for s in client.data['job']['steps'] if s['name'] == name)['conclusion'] = 'skipped'
            with self.subTest(name=name), self.assertRaises(ValueError):
                latest.validate_metadata(client, arm.REPOSITORY, [a['id'] for a in latest.ARTIFACTS],
                                         log_loader=lambda: FIXTURE['producer_log'].encode())
        with self.assertRaisesRegex(ValueError, 'exact stage3 producer log'):
            latest.validate_metadata(Client(), arm.REPOSITORY, [a['id'] for a in latest.ARTIFACTS],
                                     log_loader=lambda: FIXTURE['producer_log'].encode() + b' ')


class LatestMetadataTest(unittest.TestCase):
    def metadata(self, client=None):
        return latest.validate_metadata(client or Client(), arm.REPOSITORY, [a['id'] for a in latest.ARTIFACTS],
                                        log_loader=lambda: FIXTURE['producer_log'].encode())

    def test_actual_failed_compiled_stage3_and_two_pinned_artifacts(self):
        self.assertEqual(self.metadata(), latest.expected_manifest())
        self.assertEqual(sum(latest.VOLUME_SIZES), 10682345540)
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            for name, text in FIXTURE['files'].items():
                path = work / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            latest.donor_identity(work)
            receipt = json.loads(FIXTURE['files'][latest.previous.RECEIPT])
            self.assertEqual(arm.recipes(HISTORICAL), receipt['recipes'])
            self.assertTrue(FIXTURE['launch']['passed'])
            self.assertFalse(json.loads(FIXTURE['files'][latest.FAILURE])['ci_gate_passed'])
            handoff = work / latest.HANDOFF
            original = json.loads(handoff.read_text())
            for key, value in dict(arch='x64', continuation_run='1', continuation_stage=2,
                                   job_id=1, preparation_key='other').items():
                handoff.write_text(json.dumps(dict(original, **{key: value})))
                with self.assertRaises(ValueError):
                    latest.donor_identity(work)

    def test_reject_wrong_run_job_attempt_stage_digest_size_missing_parts(self):
        for subject, changes in {
            'run': dict(id=1, head_sha='a' * 40, run_attempt=2, head_branch='other', event='push',
                        conclusion='success', path='other.yml'),
            'job': dict(id=1, run_id=1, head_sha='a' * 40, run_attempt=2, labels=['macos-26-intel'],
                        conclusion='success', name='stage 2'),
        }.items():
            for key, value in changes.items():
                client = Client()
                client.data[subject][key] = value
                with self.subTest(subject=subject, key=key), self.assertRaises(ValueError):
                    self.metadata(client)
        for index in (0, 1):
            for key, value in dict(id=1, size_in_bytes=1, digest='sha256:' + 'a' * 64,
                                   expired=True, workflow_run={}).items():
                client = Client()
                artifacts = [a for a in client.data['artifacts'] if a['name'].startswith(latest.PREFIX)]
                artifacts[index][key] = value
                with self.assertRaises(ValueError):
                    self.metadata(client)
        client = Client()
        client.data['artifacts'] = []
        with self.assertRaises(ValueError):
            self.metadata(client)

    def test_download_requires_both_exact_published_volumes(self):
        with tempfile.TemporaryDirectory() as temporary:
            origin, report = Path(temporary) / 'origin', Path(temporary) / 'report'
            origin.write_text(json.dumps(latest.expected_manifest()))
            report.write_text(json.dumps(downloaded()))
            latest.download_identity(origin, report)
            for changes in (dict(run_id=arm.RUN), dict(publication='pending'), dict(total_size_in_bytes=1),
                            dict(artifacts=[]), dict(volumes=[])):
                report.write_text(json.dumps(dict(downloaded(), **changes)))
                with self.assertRaises(ValueError):
                    latest.download_identity(origin, report)
            for field in ('size_in_bytes', 'sha256', 'status'):
                data = downloaded()
                data['artifacts'][1]['attempts'][-1][field] = 'wrong'
                report.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    latest.download_identity(origin, report)

    def test_shell_cannot_cold_start_or_consume_receipts_without_exact_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary) / 'work'
            env = dict(os.environ, CHROMIX_UNCHANGED_RESUME='mac156-arm64-stage3',
                       CHROMIX_BUILD_PROFILE='fast', CHROMIX_USE_UPSTREAM_CACHE='1',
                       GITHUB_RUN_ID='999', GITHUB_RUN_ATTEMPT='1',
                       PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
            command = ['/bin/bash', str(ROOT / 'build/posix/ci-stage.sh'),
                       '--platform', 'macos', '--arch', 'arm64', '--workdir', str(work)]
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('unchanged mac156 continuation identity failed', result.stderr)
            self.assertFalse((work / 'src').exists())
            receipt = work / latest.RECEIPT
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text('{}')
            env['CHROMIX_UNCHANGED_RESUME'] = ''
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('dedicated continuation mode', result.stderr)
            self.assertFalse((work / 'src').exists())

    def test_native_gates_and_compiled_failure_snapshot_contract_unchanged(self):
        current = (ROOT / 'build/posix/ci-stage.sh').read_text()
        old = subprocess.check_output(['git', '-C', str(ROOT), 'show',
                                       'f6da88f:build/posix/ci-stage.sh'], text=True)
        marker = '# ---- completion checks'
        self.assertEqual(current[current.index(marker):], old[old.index(marker):])
        jobs = yaml.safe_load((ROOT / '.github/workflows/build-posix-github.yml').read_text())['jobs']
        for stage in range(1, 9):
            steps = jobs[f'posix-{stage}']['steps']
            runtime = next(s for s in steps if s.get('id') == 'runtime_checkpoint')
            self.assertIn("steps.stage.outputs.compiled_ready == 'true'", runtime['if'])
            self.assertIn('bash build/posix/ci-parts.sh', runtime['run'])
            run = next(s for s in steps if s.get('id') == 'stage')
            self.assertNotIn('continue-on-error', run)
            final = next(s for s in steps if s.get('name') == 'Upload final bundle')
            self.assertEqual(final['if'], "steps.stage.outputs.finished == 'true'")

    def test_main_entry_and_all_handoff_attempts_are_fixed(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/build-mac156-arm64-stage3-resume.yml').read_text())
        job = workflow['jobs']['resume']
        self.assertEqual(job['if'], "github.ref == 'refs/heads/main'")
        values = job['with']
        self.assertEqual(values['resume_run_id'], str(latest.RUN))
        self.assertEqual(values['resume_artifact_ids'], '13748238839,13748243740')
        self.assertEqual(values['resume_migration'], 'mac156-arm64-hevc')
        self.assertEqual(values['resume_tree_stage'], '3')
        self.assertEqual(values['max-stages'], 8)
        jobs = yaml.safe_load((ROOT / '.github/workflows/build-posix-github.yml').read_text())['jobs']
        for stage in range(1, 9):
            steps = jobs[f'posix-{stage}']['steps']
            self.assertTrue(any(s.get('if') == "inputs.resume_migration == 'mac156-arm64-hevc'"
                                and 'CHROMIX_UNCHANGED_RESUME=mac156-arm64-hevc' in s.get('run', '') for s in steps))
            if stage > 1:
                producer = next(s for s in steps if s.get('name') == 'Record exact mac156 handoff producer attempt')
                self.assertIn('CHROMIX_PREVIOUS_ATTEMPT=$PRODUCER_ATTEMPT', producer['run'])
                self.assertNotIn('linux', producer['if'])
                self.assertEqual(producer['env']['PRODUCER_ATTEMPT'],
                                 '${{ needs.posix-' + str(stage - 1) + '.outputs.snapshot_attempt }}')


if __name__ == '__main__':
    unittest.main()
