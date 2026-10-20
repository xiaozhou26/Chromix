"""ARM64 stage8 admission from actual attempt3 receipts and completed producer log."""
import copy
import gzip
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import validate_macos155_arm64_stage8_checkpoint as arm
from tools import validate_macos155_stage7_checkpoint as intel
from tools import migrate_restored_snapshot as migration
from tools.tests.macos155_historical_fixture import historical_recipes
from tools import validate_posix_snapshot as snapshots

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/macos155_arm64_stage8_receipts.json.gz').read_bytes()))
INTEL = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/macos155_stage7_receipts.json.gz').read_bytes()))
LOG = FIXTURE['producer_log'].encode()


class Client:
    def __init__(self):
        self.data = copy.deepcopy(FIXTURE)

    def get(self, path):
        return {f'/actions/runs/{arm.RUN}': self.data['run'],
                f'/actions/jobs/{arm.JOB}': self.data['job']}[path]

    def items(self, path, key):
        assert path == f'/actions/runs/{arm.RUN}/artifacts' and key == 'artifacts'
        return self.data['artifacts']


class MetadataTest(unittest.TestCase):
    def validate(self, client=None, raw=LOG):
        return arm.validate_metadata(client or Client(), arm.REPOSITORY, [arm.ARTIFACT], log_loader=lambda: raw)

    def test_actual_failed_stage8_with_stale_upload_api_and_finalized_log(self):
        report = self.validate()
        self.assertEqual((report['run_id'], report['attempt'], report['stage'], report['job_id']),
                         (39104897472, 3, 8, 118034411322))
        self.assertEqual(report['artifacts'][0]['digest'], arm.DIGEST)
        self.assertEqual(report['producer_log_sha256'], arm.JOB_LOG_HASH)
        step = next(s for s in report['producer_job']['steps'] if s['name'] == 'Upload tree part 1')
        self.assertEqual((step['status'], step['conclusion']), ('in_progress', None))
        client = Client()
        for step in client.data['job']['steps']:
            if step['name'].startswith('Upload tree part'):
                step.update(status='completed', conclusion='success')
        self.validate(client)

    def test_fixed_run_job_repository_and_architecture(self):
        cases = {
            'run': dict(id=arm.RUN + 1, head_sha='a' * 40, head_branch='main', event='push',
                        run_attempt=2, status='in_progress', conclusion='success',
                        path='.github/workflows/build-macos-x64.yml', repository={'full_name': 'foreign/repo'},
                        head_repository={'full_name': 'foreign/repo'}),
            'job': dict(id=intel.JOB, run_id=intel.RUN, head_sha='a' * 40, head_branch='main',
                        run_attempt=2, status='in_progress', conclusion='success',
                        labels=['macos-15-intel'], name='macos-x64 stage 8 (resume compile)'),
        }
        for subject, changes in cases.items():
            for field, value in changes.items():
                client = Client()
                client.data[subject][field] = value
                with self.subTest(subject=subject, field=field), self.assertRaises(ValueError):
                    self.validate(client)
        for ids in ([], [intel.ARTIFACT], [arm.ARTIFACT, arm.ARTIFACT], [arm.ARTIFACT, 1]):
            with self.assertRaises(ValueError):
                arm.validate_metadata(Client(), arm.REPOSITORY, ids, log_loader=lambda: LOG)
        with self.assertRaises(ValueError):
            arm.validate_metadata(Client(), 'foreign/repo', [arm.ARTIFACT], log_loader=lambda: LOG)

    def test_every_checkpoint_upload_step_and_pinned_log_is_required(self):
        names = ('Run stage 8', 'Verify handoff snapshot', 'Preserve latest macOS compiled checkpoint',
                 'Upload failed macOS runtime bundle', *(f'Upload tree part {n}' for n in range(1, 5)))
        for name in names:
            for change in ({'status': 'completed', 'conclusion': 'failure'}, {'status': 'pending', 'conclusion': 'success'}):
                if name == 'Run stage 8' and change['conclusion'] == 'failure':
                    change = {'status': 'completed', 'conclusion': 'success'}
                client = Client()
                next(s for s in client.data['job']['steps'] if s['name'] == name).update(change)
                with self.subTest(name=name, change=change), self.assertRaises(ValueError):
                    self.validate(client)
        for raw in (b'', LOG[:-1], LOG + b'\n', LOG.replace(b'12199650604', b'12180797398')):
            with self.assertRaisesRegex(ValueError, 'log size/SHA-256'):
                self.validate(raw=raw)

    def test_artifact_origin_hash_size_expiry_and_complete_parts(self):
        for field, value in dict(id=intel.ARTIFACT, size_in_bytes=arm.SIZE - 1, digest=intel.DIGEST,
                                 expired=True, name='chromix-mac-arm64-tree-s7-attempt-3-part1',
                                 workflow_run={'id': intel.RUN, 'head_sha': arm.SHA}).items():
            client = Client()
            client.data['artifacts'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(client)
        for artifacts in ([], Client().data['artifacts'] * 2):
            client = Client()
            client.data['artifacts'] = artifacts
            with self.assertRaises(ValueError):
                self.validate(client)

    def test_shared_cli_routes_exact_arm_tuple_and_rejects_cross_arch_workflows(self):
        with tempfile.TemporaryDirectory() as temporary:
            argv = ['validate_posix_snapshot.py', '--repository', arm.REPOSITORY,
                    '--run-id', str(arm.RUN), '--stage', '8', '--attempt', '3',
                    '--artifact-ids', str(arm.ARTIFACT), '--arch', 'arm64',
                    '--report', str(Path(temporary) / 'origin.json')]
            with patch('sys.argv', argv), patch.object(snapshots, 'Client', return_value=Client()), \
                    patch.object(arm, 'validate_metadata', return_value=self.validate()) as validate, \
                    patch.dict(os.environ, {'GITHUB_WORKFLOW': arm.WORKFLOW, 'GITHUB_OUTPUT': ''}):
                self.assertEqual(snapshots.main(), 0)
                validate.assert_called_once()
            for flag, value in (('--run-id', str(intel.RUN)), ('--stage', '7'),
                                ('--attempt', '2'), ('--arch', 'x64')):
                wrong = list(argv)
                wrong[wrong.index(flag) + 1] = value
                with patch('sys.argv', wrong), patch.object(snapshots, 'Client', return_value=Client()), \
                        patch.dict(os.environ, {'GITHUB_WORKFLOW': arm.WORKFLOW}), self.assertRaises(ValueError):
                    snapshots.main()
            with patch('sys.argv', argv), patch.object(snapshots, 'Client', return_value=Client()), \
                    patch.dict(os.environ, {'GITHUB_WORKFLOW': intel.WORKFLOW}), self.assertRaises(ValueError):
                snapshots.main()


class SourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repository = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.repository.cleanup)
        cls.repo = historical_recipes(Path(cls.repository.name) / 'repo')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name) / 'work'
        for name, data in FIXTURE['files'].items():
            self.put(name, data)
        # prepare_restored_build writes the same object to both marker and report.
        self.put('src/.chromix-restored-build-prepared.json', FIXTURE['files']['upstream-cache-preparation.json'])
        self.put('src/.chromix-source-ready', arm.KEY + '\n')
        self.put('src/chrome/VERSION', 'MAJOR=155\nMINOR=0\nBUILD=8059\nPATCH=39\n')
        self.put('src/.chromix-toolchain-ready', '')
        self.put('src/.chromix-domain-substituted', '')
        for name in arm.STATE_FILES:
            self.put('src/' + name, 'retained ARM64 stage8 Ninja state\n')
        self.put('src/out/Default/obj/retained.o', 'compiled ARM64 object\n')

    def put(self, name, data):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        return path

    def admit(self):
        with patch.object(migration, '_donor_git', side_effect=[arm.SHA.encode(), arm.TREE.encode(), b'']), \
                patch.object(migration.restore, 'verify_restored'), patch.object(migration, '_verify_tooling'), \
                patch.object(arm, 'live_source'):
            return arm.admit(self.work, self.repo, self.repo)

    def test_real_arm_receipts_and_source_hashes_not_string_substituted_intel(self):
        self.assertEqual(migration.source_ready_key(self.repo, 'macos', 'arm64'), arm.KEY)
        self.assertEqual(len(arm.source_state(self.repo, self.work, initial=True)), 3)
        for name, digest in arm.PINNED_FILES.items():
            self.assertEqual(arm.sha(FIXTURE['files'][name].encode()), digest)
        source = json.loads(FIXTURE['files']['fingerprint-diagnostics/source-final.json'])
        self.assertEqual(arm.identity_hash(source['identity']), arm.SOURCE_IDENTITY)
        self.assertEqual(arm.identity_hash(source['outputs']), arm.SOURCE_OUTPUTS)
        self.assertNotEqual(arm.UPSTREAM_IDENTITY, intel.UPSTREAM_IDENTITY)
        self.assertNotEqual(arm.PREPARATION_HASH, intel.PREPARATION_HASH)
        self.assertNotEqual(arm.KEY, intel.KEY)
        self.assertEqual(arm.SOURCE_IDENTITY, intel.SOURCE_IDENTITY)
        self.assertEqual(arm.PINNED_FILES['fingerprint-diagnostics/source-final.json'],
                         intel.PINNED_FILES['fingerprint-diagnostics/source-final.json'])

    def test_receipts_gn_and_ninja_state_cannot_be_missing_changed_or_linked(self):
        names = (*arm.PINNED_FILES, 'upstream-cache-preparation.json',
                 'src/.chromix-restored-build-prepared.json', *('src/' + n for n in arm.STATE_FILES))
        for name in names:
            path = self.work / name
            original = path.read_bytes()
            path.unlink()
            with self.subTest(name=name), self.assertRaises((ValueError, RuntimeError, OSError)):
                arm.source_state(self.repo, self.work, initial=True)
            path.symlink_to(self.work / 'src/out/Default/obj/retained.o')
            with self.assertRaises((ValueError, RuntimeError)):
                arm.source_state(self.repo, self.work, initial=True)
            path.unlink()
            path.write_bytes(original)
        for name in arm.PINNED_FILES:
            path = self.work / name
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            with self.assertRaises(ValueError):
                arm.source_state(self.repo, self.work)
            path.write_bytes(original)

    def test_intel_prepare_gn_receipts_and_invalidating_preparation_are_rejected(self):
        for name in ('src/out/Default/args.gn', 'src/.chromix-upstream-restored.json',
                     'upstream-cache-preparation.json'):
            path = self.work / name
            original = path.read_bytes()
            path.write_text(INTEL['files'][name])
            with self.subTest(name=name), self.assertRaises(ValueError):
                arm.source_state(self.repo, self.work)
            path.write_bytes(original)
        for field in ('first_finish', 'tool_swap_invalidations', 'toolchain_invalidated_outputs'):
            data = json.loads(FIXTURE['files']['upstream-cache-preparation.json'])
            data['counters'][field] = 1
            self.put('upstream-cache-preparation.json', json.dumps(data))
            with self.assertRaises(ValueError):
                arm.preparation(self.work)
        self.put('upstream-cache-preparation.json', FIXTURE['files']['upstream-cache-preparation.json'])
        self.put('src/.chromix-source-ready', intel.KEY)
        with self.assertRaises(RuntimeError):
            arm.source_state(self.repo, self.work)

    def test_admission_read_only_and_continuation_logical9_to16(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.work / 'src').rglob('*') if p.is_file()}
        receipt = self.admit()
        self.assertEqual((receipt['arch'], receipt['stage'], receipt['attempt']), ('arm64', 8, 3))
        self.assertEqual(receipt['changed_files'], [])
        with patch.dict(os.environ, {'CHROMIX_BUILD_PROFILE': 'fast', 'CHROMIX_USE_UPSTREAM_CACHE': '1',
                                     'GITHUB_RUN_ID': 'new-arm-run', 'GITHUB_RUN_ATTEMPT': '1'}), \
                patch.object(arm, 'live_source'):
            start = arm.continuation(self.repo, self.work, 1, 'before')
            end = arm.continuation(self.repo, self.work, 8, 'after')
            self.assertEqual((start['logical_stage'], end['logical_stage']), (9, 16))
            self.assertEqual(start['run_id'], arm.RUN)
            self.assertEqual(start['continuation_run'], 'new-arm-run')
            self.put('src/out/Default/.ninja_log', 'incremental compilation advanced')
            arm.continuation(self.repo, self.work, 2, 'after')
            with self.assertRaisesRegex(ValueError, 'initial Ninja state'):
                arm.continuation(self.repo, self.work, 1, 'before')
        for path, state in before.items():
            if path.name != '.ninja_log':
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), state)

    def test_no_cold_fallback_or_cross_arch_provenance(self):
        env = {'CHROMIX_BUILD_PROFILE': 'fast', 'CHROMIX_USE_UPSTREAM_CACHE': '1'}
        with patch.dict(os.environ, env), self.assertRaises((ValueError, OSError)):
            arm.continuation(self.repo, self.work, 1, 'before')
        receipt = self.admit()
        for field, value in (('arch', 'x64'), ('run_id', intel.RUN), ('stage', 7),
                             ('attempt', 2), ('preparation_key', intel.KEY)):
            self.put(arm.RECEIPT, json.dumps(dict(receipt, **{field: value})))
            with patch.dict(os.environ, env), self.assertRaises(ValueError):
                arm.continuation(self.repo, self.work, 1, 'before')
        self.put(arm.RECEIPT, json.dumps(receipt))
        for change in ({'CHROMIX_BUILD_PROFILE': 'release'}, {'CHROMIX_USE_UPSTREAM_CACHE': '0'}):
            with patch.dict(os.environ, dict(env, **change)), self.assertRaises(ValueError):
                arm.continuation(self.repo, self.work, 1, 'before')

    def test_live_source_requires_real_patch_identity_outputs_and_version(self):
        output = self.work / 'live.json'
        original = json.loads(FIXTURE['files']['fingerprint-diagnostics/source-final.json'])
        for field, value in ((None, None), ('status', 'failed'), ('patch_count', 225),
                             ('identity', {}), ('outputs', {}), ('domain_substituted', False)):
            data = dict(original)
            if field:
                data[field] = value
            output.unlink(missing_ok=True)
            with patch.object(arm.subprocess, 'run', side_effect=lambda *a, **k: output.write_text(json.dumps(data))):
                if field:
                    with self.assertRaises(ValueError):
                        arm.live_source(self.repo, self.work, output)
                else:
                    arm.live_source(self.repo, self.work, output)

    def test_donor_sha_tree_dirty_checkout_and_recipe_change_are_rejected(self):
        for responses in ([b'a' * 40], [arm.SHA.encode(), b'b' * 40],
                          [arm.SHA.encode(), arm.TREE.encode(), b' M build/args.macos.gn']):
            with patch.object(migration, '_donor_git', side_effect=responses), self.assertRaises(ValueError):
                arm.admit(self.work, self.repo, self.repo)
        with patch.object(migration, 'source_ready_key', return_value=intel.KEY), self.assertRaises(ValueError):
            arm.recipes(self.repo)


    def test_new_controller_recipe_is_not_an_unchanged_source_exception(self):
        with self.assertRaisesRegex(ValueError, 'preparation key changed'):
            arm.recipes(ROOT, self.repo)
        with self.assertRaisesRegex(ValueError, 'preparation key changed'):
            arm.source_state(ROOT, self.work)
        self.admit()
        with patch.dict(os.environ, {'CHROMIX_BUILD_PROFILE': 'fast', 'CHROMIX_USE_UPSTREAM_CACHE': '1'}), \
                self.assertRaisesRegex(ValueError, 'preparation key changed'):
            arm.continuation(ROOT, self.work, 1, 'before')


class WorkflowTest(unittest.TestCase):
    def test_entry_uses_native_arm64_fixed_donor_and_existing_acceptance(self):
        text = (ROOT / '.github/workflows/build-mac155-arm64-stage8-resume.yml').read_text()
        for token in ('uses: ./.github/workflows/build-posix-github.yml', 'runner: macos-15\n',
                      'arch: arm64', 'artifact: chromix-mac-arm64', 'build_profile: fast', 'max-stages: 8',
                      "resume_run_id: '39104897472'", "resume_tree_stage: '8'", "resume_attempt: '3'",
                      "resume_artifact_ids: '12199650604'", 'use_upstream_cache: true', 'logical stages 9..16'):
            self.assertIn(token, text)
        self.assertNotIn('push:', text)
        self.assertNotIn('x64', text)
        stage = (ROOT / 'build/posix/ci-stage.sh').read_text()
        for token in ('macos_browser_smoke.py', 'fingerprint_acceptance.py', 'emit runtime_verified true'):
            self.assertIn(token, stage)

    def test_cli_admission_routes_workflow_and_rejects_wrong_arch(self):
        for helper, arch in ((arm, 'arm64'), (intel, 'x64')):
            with patch.dict(os.environ, {'GITHUB_WORKFLOW': helper.WORKFLOW}), \
                    patch.object(helper, 'admit', return_value={'status': 'verified'}) as admit, \
                    patch.object(migration, 'migrate') as migrate:
                args = ['--repo', str(ROOT), '--previous-repo', '/donor', '--workdir', '/work',
                        '--platform', 'macos', '--arch', arch]
                self.assertEqual(migration.main(args), 0)
                admit.assert_called_once()
                migrate.assert_not_called()
                args[-1] = 'x64' if arch == 'arm64' else 'arm64'
                self.assertEqual(migration.main(args), 1)

    def test_missing_or_cross_arch_receipt_stops_before_cold_fetch(self):
        for workflow, arch, receipt in ((arm.WORKFLOW, 'arm64', None),
                                        (intel.WORKFLOW, 'x64', arm.RECEIPT),
                                        (arm.WORKFLOW, 'arm64', intel.RECEIPT),
                                        (arm.WORKFLOW, 'x64', None)):
            with tempfile.TemporaryDirectory() as temporary:
                if receipt:
                    path = Path(temporary) / receipt
                    path.parent.mkdir(parents=True)
                    path.write_text('{}')
                result = subprocess.run(['bash', str(ROOT / 'build/posix/ci-stage.sh'),
                                         '--platform', 'macos', '--arch', arch, '--workdir', temporary],
                                        capture_output=True, text=True,
                                        env={**os.environ, 'GITHUB_WORKFLOW': workflow,
                                             'CHROMIX_BUILD_PROFILE': 'fast', 'CHROMIX_USE_UPSTREAM_CACHE': '1'})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((Path(temporary) / 'src').exists())

    def test_arm_compile_failure_retains_valid_snapshot_and_failure_status(self):
        from tools.tests.test_posix_stages import PosixStageHandoffExecutionTest
        harness = PosixStageHandoffExecutionTest()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        harness.put(harness.bindir / 'python3', '#!/bin/sh\nprintf "guard %s\\n" "$*" >> "$CALL_LOG"\nexit 0\n')
        with patch.dict(os.environ, {'GITHUB_WORKFLOW': arm.WORKFLOW}):
            result = harness.run_stage('macos', 'arm64', 'fast', 1, phase='timeout', build_rc=42)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('upload_snapshot=true', harness.output.read_text())
        self.assertNotIn('finished=true', harness.output.read_text())
        calls = harness.calls.read_text()
        self.assertEqual(calls.count('validate_macos155_arm64_stage8_checkpoint.py'), 2)
        self.assertNotIn('validate_macos155_stage7_checkpoint.py', calls)
        self.assertLess(calls.index('--phase before'), calls.index('build macos'))
        self.assertLess(calls.index('--phase after'), calls.index('snapshot'))


if __name__ == '__main__':
    unittest.main()
