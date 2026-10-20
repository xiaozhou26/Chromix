"""Exact Intel155 continuation admission using recorded stage7 diagnostics, never a cold tree."""
import copy
import gzip
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import validate_macos155_stage7_checkpoint as resume
from tools import validate_posix_snapshot as snapshots
from tools import migrate_restored_snapshot as migration
from tools.tests.macos155_historical_fixture import historical_recipes

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/macos155_stage7_receipts.json.gz').read_bytes()))


class Client:
    def __init__(self):
        self.data = copy.deepcopy(FIXTURE)
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if path == f'/actions/runs/{resume.RUN}':
            return self.data['run']
        return next(job for job in self.data['jobs'] if path == f"/actions/jobs/{job['id']}")

    def items(self, path, key):
        self.calls.append(path)
        return self.data[key]


class MetadataTest(unittest.TestCase):
    def validate(self, client):
        return resume.validate_metadata(client, resume.REPOSITORY, [resume.ARTIFACT])

    def test_actual_stage7_success_of_failed_attempt2_is_accepted(self):
        client = Client()
        report = self.validate(client)
        self.assertEqual(report['head_sha'], resume.SHA)
        self.assertEqual(report['job_id'], resume.JOB)
        self.assertEqual(report['stage'], 7)
        self.assertEqual(report['attempt'], 2)
        self.assertEqual(report['artifacts'][0]['digest'], resume.DIGEST)
        self.assertIn(f'/actions/runs/{resume.RUN}/attempts/2/jobs', client.calls)

    def test_repository_and_complete_exact_artifact_set(self):
        for ids in ([], [1], [resume.ARTIFACT, resume.ARTIFACT], [resume.ARTIFACT, 1]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                resume.validate_metadata(Client(), resume.REPOSITORY, ids)
        with self.assertRaises(ValueError):
            resume.validate_metadata(Client(), 'foreign/Chromix', [resume.ARTIFACT])

    def test_run_identity_is_fixed(self):
        for field, value in dict(head_sha='a' * 40, head_branch='main', event='push',
                                 run_attempt=3, conclusion='success', status='in_progress',
                                 path='.github/workflows/build-macos-arm64.yml').items():
            client = Client()
            client.data['run'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(client)

    def test_job_identity_and_every_handoff_step(self):
        for index in range(2):
            for field, value in dict(run_id=1, head_sha='b' * 40, run_attempt=1,
                                     status='in_progress', conclusion='cancelled').items():
                client = Client()
                client.data['jobs'][index][field] = value
                with self.subTest(index=index, field=field), self.assertRaises(ValueError):
                    self.validate(client)
            for step in Client().data['jobs'][index]['steps']:
                if not step['name'].startswith(('Run stage', 'Verify handoff', 'Upload tree')):
                    continue
                client = Client()
                target = next(s for s in client.data['jobs'][index]['steps'] if s['name'] == step['name'])
                target['conclusion'] = 'cancelled'
                with self.subTest(index=index, step=step['name']), self.assertRaises(ValueError):
                    self.validate(client)

    def test_artifact_hash_size_origin_attempt_expiry_and_missing_parts(self):
        for field, value in dict(id=1, size_in_bytes=resume.SIZE + 1, expired=True,
                                 digest='sha256:' + 'f' * 64,
                                 name='chromix-mac-x64-tree-s7-attempt-1-part1',
                                 workflow_run={'id': resume.RUN, 'head_sha': 'a' * 40}).items():
            client = Client()
            client.data['artifacts'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(client)
        client = Client()
        client.data['artifacts'] *= 2
        with self.assertRaises(ValueError):
            self.validate(client)

    def test_no_silent_switch_to_stage8(self):
        client = Client()
        client.data['artifacts'].append(dict(client.data['artifacts'][0], id=123,
                                             name='chromix-mac-x64-tree-s8-attempt-2-part1'))
        with self.assertRaisesRegex(ValueError, 'stage8 artifact exists'):
            self.validate(client)

    def test_shared_validator_routes_exact_run_not_current_branch(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / 'origin.json'
            argv = ['validate_posix_snapshot.py', '--repository', resume.REPOSITORY,
                    '--run-id', str(resume.RUN), '--stage', '7', '--attempt', '2',
                    '--artifact-ids', str(resume.ARTIFACT), '--arch', 'x64',
                    '--recovery-branch', 'independent/recovery', '--report', str(report)]
            with patch('sys.argv', argv), patch.object(snapshots, 'Client', return_value=Client()), \
                    patch.dict(os.environ, {'GITHUB_OUTPUT': ''}):
                self.assertEqual(snapshots.main(), 0)
                self.assertEqual(json.loads(report.read_text())['head_sha'], resume.SHA)
            argv[argv.index('--stage') + 1] = '8'
            with patch('sys.argv', argv), patch.object(snapshots, 'Client', return_value=Client()), \
                    self.assertRaises(ValueError):
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
        self.donor = self.repo
        for name, data in FIXTURE['files'].items():
            self.put(name, data)
        self.put('src/.chromix-source-ready', resume.KEY + '\n')
        self.put('src/chrome/VERSION', 'MAJOR=155\nMINOR=0\nBUILD=8059\nPATCH=39\n')
        self.put('src/.chromix-toolchain-ready', '')
        self.put('src/.chromix-domain-substituted', '')
        for name in resume.STATE_FILES:
            self.put('src/' + name, 'retained native Ninja state\n')
        self.put('src/out/Default/obj/retained.o', 'compiled stage7 object\n')

    def put(self, name, data):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        return path

    def test_recorded_receipts_and_real_recipe_key_are_mac155_not_mac154(self):
        state = resume.source_state(self.repo, self.work, initial=True)
        self.assertEqual(set(state), set(resume.STATE_FILES))
        self.assertEqual(migration.source_ready_key(self.repo, 'macos', 'x64'), resume.KEY)
        source = json.loads(FIXTURE['files']['fingerprint-diagnostics/source-final.json'])
        self.assertEqual(resume.identity_hash(source['identity']), resume.SOURCE_IDENTITY)
        self.assertEqual(resume.identity_hash(source['outputs']), resume.SOURCE_OUTPUTS)
        self.assertEqual(source['patch_count'], 224)

    def test_exact_donor_checkout_and_unchanged_recipe_are_required(self):
        self.assertEqual(resume.recipes(self.repo, self.repo), resume.recipes(self.repo))
        original_read = resume.read
        def changed_read(root, name):
            data = original_read(self.repo, name)
            return data + b'changed' if root == self.work else data
        with patch.object(resume, 'read', side_effect=changed_read), self.assertRaises(ValueError):
            resume.recipes(self.repo, self.work)
        for head, tree, dirty in ((b'a' * 40, resume.TREE.encode(), b''),
                                   (resume.SHA.encode(), b'b' * 40, b''),
                                   (resume.SHA.encode(), resume.TREE.encode(), b' M build/args.macos.gn')):
            with patch.object(migration, '_donor_git', side_effect=[head, tree, dirty]), \
                    self.assertRaises(ValueError):
                resume.admit(self.work, self.donor, self.repo)

    def test_each_source_gn_and_prepare_receipt_is_pinned(self):
        for name in (*resume.PINNED_FILES, 'upstream-cache-preparation.json',
                     'src/.chromix-restored-build-prepared.json'):
            path = self.work / name
            data = path.read_bytes()
            path.write_bytes(data + b' ')
            with self.subTest(name=name), self.assertRaises(ValueError):
                resume.source_state(self.repo, self.work, initial=True)
            path.write_bytes(data)

    def test_missing_or_symlinked_ninja_state_and_interrupted_preparation_fail(self):
        for name in resume.STATE_FILES:
            path = self.work / 'src' / name
            data = path.read_bytes()
            path.unlink()
            with self.assertRaises((ValueError, OSError)):
                resume.source_state(self.repo, self.work)
            path.symlink_to(self.work / 'src/out/Default/obj/retained.o')
            with self.assertRaises(ValueError):
                resume.source_state(self.repo, self.work)
            path.unlink()
            path.write_bytes(data)
        for name in (migration.TRANSACTION, resume.arp.IN_PROGRESS):
            path = self.put('src/' + name, 'interrupted')
            with self.assertRaises(ValueError):
                resume.source_state(self.repo, self.work)
            path.unlink()
        self.put('src/.chromix-source-ready', resume.KEY.replace('155.', '154.'))
        with self.assertRaises(RuntimeError):
            resume.source_state(self.repo, self.work)

    def test_repreparation_or_invalidating_cached_objects_is_forbidden(self):
        name = 'upstream-cache-preparation.json'
        original = json.loads(FIXTURE['files'][name])
        for field in ('first_finish', 'tool_swap_invalidations', 'toolchain_invalidated_outputs'):
            changed = copy.deepcopy(original)
            changed['counters'][field] = 1
            self.put(name, json.dumps(changed))
            with self.subTest(field=field), self.assertRaises(ValueError):
                resume.preparation(self.work)
        changed = copy.deepcopy(original)
        changed['source_identity']['chromium_version'] = '154.0.8037.97'
        self.put(name, json.dumps(changed))
        with self.assertRaises(ValueError):
            resume.preparation(self.work)

    def admit(self):
        def git(donor, repo, *args):
            return {'HEAD': resume.SHA.encode(), 'HEAD^{tree}': resume.TREE.encode()}.get(args[-1], b'')
        with patch.object(migration, '_donor_git', side_effect=git), \
                patch.object(migration.restore, 'verify_restored'), \
                patch.object(migration, '_verify_tooling'), patch.object(resume, 'live_source'):
            return resume.admit(self.work, self.donor, self.repo)

    def test_admission_and_continuation_never_rewrite_source_gn_or_objects(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.work / 'src').rglob('*') if p.is_file()}
        result = self.admit()
        self.assertEqual(result['changed_files'], [])
        self.assertEqual(result['preparation_key'], resume.KEY)
        env = {'CHROMIX_BUILD_PROFILE': 'fast', 'CHROMIX_USE_UPSTREAM_CACHE': '1',
               'GITHUB_RUN_ID': 'new-run', 'GITHUB_RUN_ATTEMPT': '1'}
        with patch.dict(os.environ, env), patch.object(resume, 'live_source'):
            report = resume.continuation(self.repo, self.work, 1, 'before')
            self.assertEqual(report['logical_stage'], 8)
            self.assertEqual(report['run_id'], resume.RUN)
            self.assertEqual(report['continuation_run'], 'new-run')
            self.put('src/out/Default/.ninja_log', 'new incremental progress')
            resume.continuation(self.repo, self.work, 8, 'after')
            with self.assertRaisesRegex(ValueError, 'initial Ninja state changed'):
                resume.continuation(self.repo, self.work, 1, 'before')
        for path, (data, mtime) in before.items():
            if path.name != '.ninja_log':
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (data, mtime))

    def test_continuation_fails_closed_without_provenance_or_with_cold_release_mode(self):
        env = {'CHROMIX_BUILD_PROFILE': 'fast', 'CHROMIX_USE_UPSTREAM_CACHE': '1'}
        with patch.dict(os.environ, env), self.assertRaises((ValueError, OSError)):
            resume.continuation(self.repo, self.work, 2, 'before')
        self.admit()
        for change in ({'CHROMIX_BUILD_PROFILE': 'release'}, {'CHROMIX_USE_UPSTREAM_CACHE': '0'}):
            with patch.dict(os.environ, dict(env, **change)), self.assertRaises(ValueError):
                resume.continuation(self.repo, self.work, 2, 'before')
        receipt = json.loads((self.work / resume.RECEIPT).read_text())
        receipt['head_sha'] = 'a' * 40
        self.put(resume.RECEIPT, json.dumps(receipt))
        with patch.dict(os.environ, env), self.assertRaises(ValueError):
            resume.continuation(self.repo, self.work, 2, 'before')

    def test_live_source_checks_exact_recorded_identity_and_outputs(self):
        output = self.work / 'live-source.json'
        source = json.loads(FIXTURE['files']['fingerprint-diagnostics/source-final.json'])
        for field, value in ((None, None), ('status', 'failed'), ('patch_count', 225),
                             ('identity', {}), ('outputs', {}), ('domain_substituted', False)):
            data = dict(source)
            if field:
                data[field] = value
            def run(*args, **kwargs):
                output.write_text(json.dumps(data))
            output.unlink(missing_ok=True)
            with patch.object(resume.subprocess, 'run', side_effect=run):
                if field:
                    with self.subTest(field=field), self.assertRaises(ValueError):
                        resume.live_source(self.repo, self.work, output)
                else:
                    resume.live_source(self.repo, self.work, output)


    def test_new_controller_recipe_is_not_an_unchanged_source_exception(self):
        with self.assertRaisesRegex(ValueError, 'preparation key changed'):
            resume.recipes(ROOT, self.repo)
        with self.assertRaisesRegex(ValueError, 'preparation key changed'):
            resume.source_state(ROOT, self.work)
        self.admit()
        with patch.dict(os.environ, {'CHROMIX_BUILD_PROFILE': 'fast', 'CHROMIX_USE_UPSTREAM_CACHE': '1'}), \
                self.assertRaisesRegex(ValueError, 'preparation key changed'):
            resume.continuation(ROOT, self.work, 1, 'before')


class StageExecutionTest(unittest.TestCase):
    def test_failed_compile_preserves_snapshot_but_never_reports_success(self):
        from tools.tests.test_posix_stages import PosixStageHandoffExecutionTest
        harness = PosixStageHandoffExecutionTest()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        harness.put(harness.bindir / 'python3', '#!/bin/sh\n'
                    'printf "guard %s\\n" "$*" >> "$CALL_LOG"\nexit "$GUARD_RC"\n')
        original_run = harness.run_stage
        env = {'GITHUB_WORKFLOW': resume.WORKFLOW, 'GUARD_RC': '0'}
        with patch.dict(os.environ, env):
            result = original_run('macos', 'x64', 'fast', 1, phase='timeout', build_rc=42)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('upload_snapshot=true', harness.output.read_text())
        self.assertNotIn('finished=true', harness.output.read_text())
        calls = harness.calls.read_text()
        self.assertLess(calls.index('--phase before'), calls.index('build macos'))
        self.assertLess(calls.index('--phase after'), calls.index('snapshot'))

    def test_missing_admission_stops_before_prepare_fetch_or_compile(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(['bash', str(ROOT / 'build/posix/ci-stage.sh'),
                                     '--platform', 'macos', '--arch', 'x64',
                                     '--workdir', temporary], capture_output=True, text=True,
                                    env={**os.environ, 'GITHUB_WORKFLOW': resume.WORKFLOW,
                                         'CHROMIX_BUILD_PROFILE': 'fast', 'CHROMIX_USE_UPSTREAM_CACHE': '1'})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('cold fallback forbidden', result.stderr)
            self.assertFalse((Path(temporary) / 'src').exists())


class WorkflowTest(unittest.TestCase):
    def test_minimal_fixed_entry_reuses_all_eight_stages_and_native_acceptance(self):
        workflow = (ROOT / '.github/workflows/build-mac155-x64-stage7-resume.yml').read_text()
        for text in ('uses: ./.github/workflows/build-posix-github.yml', 'runner: macos-15-intel',
                     'max-stages: 8', 'build_profile: fast', 'use_upstream_cache: true',
                     "resume_run_id: '39104895441'", "resume_tree_stage: '7'",
                     "resume_attempt: '2'", "resume_artifact_ids: '12180797398'",
                     'isolated continuation 1..8', 'cancel-in-progress: false'):
            self.assertIn(text, workflow)
        self.assertNotIn('push:', workflow)
        self.assertNotIn('154', workflow)
        stage = (ROOT / 'build/posix/ci-stage.sh').read_text()
        self.assertLess(stage.index('--phase before'), stage.index('fetch-upstream-cache.sh'))
        self.assertLess(stage.index('--phase after'), stage.index('if [ "$RC" -eq 124 ]'))
        for text in ('macos_browser_smoke.py', 'fingerprint_acceptance.py',
                     'emit runtime_verified true', 'emit upload_snapshot true'):
            self.assertIn(text, stage)
        self.assertEqual(subprocess.run(['bash', '-n', str(ROOT / 'build/posix/ci-stage.sh')]).returncode, 0)

    def test_exact_donor_bypasses_migration_not_native_build(self):
        with patch.dict(os.environ, {'GITHUB_WORKFLOW': resume.WORKFLOW}), \
                patch.object(resume, 'admit', return_value={'status': 'verified'}) as admit, \
                patch.object(migration, 'migrate') as migrate:
            self.assertEqual(migration.main(['--workdir', '/work', '--previous-repo', '/donor',
                                             '--repo', str(ROOT), '--platform', 'macos', '--arch', 'x64']), 0)
            admit.assert_called_once()
            migrate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
