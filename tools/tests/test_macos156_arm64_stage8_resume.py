"""Fixed mac156 latest-stage8 provenance and unchanged-source continuation contracts."""
import copy
import gzip
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from tools import validate_macos156_arm64_stage8_checkpoint as arm

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads(gzip.decompress(
    (ROOT / 'tools/tests/fixtures/macos156_arm64_stage8_receipts.json.gz').read_bytes()))
ENV = {'GITHUB_RUN_ID': '999999', 'GITHUB_RUN_ATTEMPT': '1',
       'CHROMIX_BUILD_PROFILE': 'fast', 'CHROMIX_USE_UPSTREAM_CACHE': '1', 'CHROMIX_JOBS': '3'}


class Client:
    def __init__(self):
        self.data = copy.deepcopy(FIXTURE)

    def get(self, path):
        return {f'/actions/runs/{arm.RUN}': self.data['run'],
                f'/actions/jobs/{arm.JOB}': self.data['job']}[path]

    def items(self, path, key):
        assert path == f'/actions/runs/{arm.RUN}/artifacts' and key == 'artifacts'
        return [self.data['artifact']]


def metadata(client=None, raw=None):
    return arm.validate_metadata(client or Client(), arm.REPOSITORY, [arm.ARTIFACT],
                                 log_loader=lambda: raw if raw is not None else FIXTURE['producer_log'].encode())


def downloaded():
    manifest = arm.expected_manifest()
    artifact = dict(manifest['artifacts'][0], attempts=[{
        'status': 'success', 'size_in_bytes': arm.SIZE, 'sha256': arm.DIGEST.split(':')[1]}])
    return dict(status='success', phase='complete', publication='published',
                repository=arm.REPOSITORY, run_id=arm.RUN, head_sha=arm.SHA,
                artifacts=[artifact], total_size_in_bytes=9643477952,
                volumes=[dict(name='tree.tar.zst.001', artifact_id=arm.ARTIFACT,
                              size_in_bytes=9643477952, sha256='a' * 64)])


class MetadataTest(unittest.TestCase):
    def test_actual_stage8_budget_exhaustion_one_volume(self):
        self.assertEqual(metadata(), arm.expected_manifest())
        self.assertEqual(arm.sha(FIXTURE['producer_log'].encode()), arm.JOB_LOG_HASH)
        self.assertIn('packed 9643477952 bytes as 1 volumes', FIXTURE['producer_log'])

    def test_rejects_foreign_or_old_run_job_artifact(self):
        cases = {
            'run': dict(id=1, head_sha='a' * 40, head_branch='other', event='push', run_attempt=2,
                        status='in_progress', conclusion='success', path='old.yml',
                        repository={'full_name': 'foreign/repo'}, head_repository={'full_name': 'foreign/repo'}),
            'job': dict(id=1, run_id=1, head_sha='a' * 40, head_branch='other', run_attempt=2,
                        status='in_progress', conclusion='success', labels=['macos-26-intel'], name='stage 7'),
            'artifact': dict(id=1, name='chromix-mac-arm64-tree-s8-attempt-1-part2',
                             size_in_bytes=arm.SIZE - 1, digest='sha256:' + 'a' * 64, expired=True),
        }
        for subject, changes in cases.items():
            for field, value in changes.items():
                with self.subTest(subject=subject, field=field):
                    client = Client()
                    client.data[subject][field] = value
                    with self.assertRaises(ValueError):
                        metadata(client)
        for raw in (b'', FIXTURE['producer_log'].encode() + b'\n'):
            with self.assertRaises(ValueError):
                metadata(raw=raw)

    def test_no_stale_upload_or_multi_volume_exception(self):
        for part in range(1, 5):
            client = Client()
            step = next(s for s in client.data['job']['steps'] if s['name'] == f'Upload tree part {part}')
            step.update(status='pending', conclusion=None)
            with self.assertRaises(ValueError):
                metadata(client)
        client = Client()
        with patch.object(client, 'items', return_value=[client.data['artifact']] * 2):
            with self.assertRaises(ValueError):
                metadata(client)


class ReceiptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checkout = tempfile.TemporaryDirectory()
        cls.donor = Path(cls.checkout.name) / 'donor'
        subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout', str(ROOT), str(cls.donor)], check=True)
        subprocess.run(['git', '-C', str(cls.donor), 'checkout', '--quiet', arm.SHA], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.checkout.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        for name, text in FIXTURE['files'].items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        for name, data in {
            'src/.chromix-restored-build-prepared.json': FIXTURE['files']['upstream-cache-preparation.json'],
            'src/.chromix-source-ready': arm.KEY + '\n', 'src/.chromix-toolchain-ready': '',
            'src/.chromix-domain-substituted': '', 'src/chrome/VERSION': 'MAJOR=156\nMINOR=0\nBUILD=8078\nPATCH=79\n',
            'src/out/Default/build.ninja': '# synthetic graph for read-only unit test\n',
            'src/out/Default/obj/test.ninja': '# synthetic subgraph\n',
            'src/out/Default/.ninja_log': '# ninja log v7\n',
            'src/out/Default/.ninja_deps': '# ninjadeps\n\x04\x00\x00\x00',
        }.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
        self.origin = self.work / 'origin.json'
        self.download = self.work / 'download.json'
        self.origin.write_text(json.dumps(arm.expected_manifest()))
        self.download.write_text(json.dumps(downloaded()))
        self.enterContext(patch.dict(os.environ, ENV))

    def admit(self):
        with patch.object(arm, 'native_state'), patch.object(arm, 'live_source'), \
                patch.object(arm.migration, '_verify_tooling'), \
                patch.object(arm.migration.restore, 'verify_restored'):
            return arm.admit(self.work, self.donor, ROOT, self.origin, self.download)

    def test_real_preimages_and_selected_226_patch_recipe(self):
        for name, digest in arm.PINNED_FILES.items():
            self.assertEqual(arm.sha((self.work / name).read_bytes()), digest, name)
        source = json.loads(FIXTURE['files']['fingerprint-diagnostics/source-final.json'])
        self.assertEqual(source['patch_count'], 226)
        self.assertEqual(arm.identity_hash(source['identity']), arm.SOURCE_IDENTITY)
        self.assertEqual(arm.identity_hash(source['outputs']), arm.SOURCE_OUTPUTS)
        self.assertEqual(arm.recipes(ROOT, self.donor), arm.recipes(ROOT))
        self.assertEqual(arm.migration.source_ready_key(self.donor, 'macos', 'arm64'), arm.KEY)
        self.assertEqual(arm.arp.patch_selection.select(ROOT, 'macos'),
                         arm.arp.patch_selection.select(self.donor, 'macos'))

    def test_reviewed_linux_only_function_exception_not_source_sha_trust(self):
        name = 'tools/prepare_restored_build.py'
        old = (self.donor / name).read_bytes()
        new = (ROOT / name).read_bytes()
        self.assertNotEqual(old, new)
        self.assertEqual(arm.recipe_bytes(name, old), arm.recipe_bytes(name, new))
        for bad in (new + b'\n', new.replace(b'def environment_identity', b'def different_identity')):
            with self.assertRaises(ValueError):
                arm.recipe_bytes(name, bad)

    def test_download_manifest_size_digest_attempt_and_single_volume(self):
        arm.download_identity(self.origin, self.download)
        for field, value in dict(attempt=2, stage=7, run_id=1, job_id=1, head_sha='a' * 40).items():
            altered = arm.expected_manifest()
            altered[field] = value
            self.origin.write_text(json.dumps(altered))
            with self.assertRaises(ValueError):
                arm.download_identity(self.origin, self.download)
        self.origin.write_text(json.dumps(arm.expected_manifest()))
        cases = [dict(status='verified'), dict(publication='unconfirmed'), dict(phase='extract'),
                 dict(total_size_in_bytes=1), dict(volumes=[]), dict(run_id=1)]
        for change in cases:
            self.download.write_text(json.dumps(dict(downloaded(), **change)))
            with self.assertRaises(ValueError):
                arm.download_identity(self.origin, self.download)
        for key, value in [('size_in_bytes', 1), ('sha256', 'f' * 64), ('status', 'failed')]:
            data = downloaded()
            data['artifacts'][0]['attempts'][-1][key] = value
            self.download.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                arm.download_identity(self.origin, self.download)

    def test_admission_changes_no_source_gn_output_or_preparation(self):
        before = arm.output_inventory(self.work / 'src')
        result = self.admit()
        self.assertEqual(before, arm.output_inventory(self.work / 'src'))
        self.assertEqual(result['changed_files'], [])
        self.assertEqual(result['artifact_size'], arm.SIZE)
        self.assertEqual(result['provenance']['manifest'], arm.expected_manifest())
        with self.assertRaises(ValueError):
            self.admit()

    def test_receipts_markers_and_gn_preimage_fail_closed(self):
        for name in (*arm.PINNED_FILES, 'src/.chromix-restored-build-prepared.json',
                     'src/.chromix-source-ready', 'src/chrome/VERSION'):
            path = self.work / name
            original = path.read_bytes()
            path.write_bytes(original + b'changed')
            with self.subTest(name=name), self.assertRaises((ValueError, RuntimeError)):
                arm.source_state(ROOT, self.work)
            path.write_bytes(original)
        for name in (arm.migration.TRANSACTION, arm.arp.IN_PROGRESS):
            path = self.work / 'src' / name
            path.touch()
            with self.assertRaises(ValueError):
                arm.source_state(ROOT, self.work)
            path.unlink()
        target = self.work / 'src/out/Default/args.gn'
        target.unlink()
        target.symlink_to(self.work / 'src/.chromix-source-ready')
        with self.assertRaises(ValueError):
            arm.source_state(ROOT, self.work)

    def test_continuation_isolated_stages_and_graph_immutable(self):
        self.admit()
        with patch.object(arm, 'native_state'), patch.object(arm, 'live_source'):
            for stage in range(1, 9):
                before = arm.continuation(ROOT, self.work, stage, 'before')
                self.assertEqual(before['logical_stage'], stage + 8)
                arm.continuation(ROOT, self.work, stage, 'after')
            for stage in (0, 9):
                with self.assertRaises(ValueError):
                    arm.continuation(ROOT, self.work, stage, 'before')
            with patch.dict(os.environ, GITHUB_RUN_ID='888888'):
                with self.assertRaises(ValueError):
                    arm.continuation(ROOT, self.work, 2, 'before')
            with patch.dict(os.environ, CHROMIX_USE_UPSTREAM_CACHE='0'):
                with self.assertRaises(ValueError):
                    arm.continuation(ROOT, self.work, 2, 'before')
            path = self.work / 'src/out/Default/obj/test.ninja'
            path.write_text('changed graph')
            with self.assertRaises(ValueError):
                arm.continuation(ROOT, self.work, 2, 'before')

    def test_live_source_rejects_old_count_identity_or_output_hash(self):
        original = json.loads(FIXTURE['files']['fingerprint-diagnostics/source-final.json'])
        for index, changes in enumerate(({}, {'patch_count': 224}, {'status': 'failed'},
                                          {'identity': {}}, {'outputs': {}}, {'domain_substituted': False})):
            output = self.work / f'live-{index}.json'
            def verify(*args, **kwargs):
                output.write_text(json.dumps(dict(original, **changes)))
            with patch.object(arm.subprocess, 'run', side_effect=verify):
                if changes:
                    with self.assertRaises(ValueError):
                        arm.live_source(ROOT, self.work, output)
                else:
                    arm.live_source(ROOT, self.work, output)

    def test_missing_handoff_and_same_run_changed_state_rejected(self):
        self.admit()
        with patch.object(arm, 'native_state'), patch.object(arm, 'live_source'):
            with self.assertRaises(ValueError):
                arm.continuation(ROOT, self.work, 2, 'before')
            arm.continuation(ROOT, self.work, 1, 'before')
            arm.continuation(ROOT, self.work, 1, 'after')
            (self.work / 'src/out/Default/.ninja_log').write_text('# ninja log v7\nchanged')
            with self.assertRaises(ValueError):
                arm.continuation(ROOT, self.work, 2, 'before')

    def test_compile_is_native_ninja_only_no_prepare_or_gn(self):
        self.admit()
        with patch.object(arm, 'native_state'), patch.object(arm, 'live_source'):
            arm.continuation(ROOT, self.work, 1, 'before')
        with patch.object(arm.shutil, 'which', return_value='/usr/bin/ninja'), \
                patch.object(arm.restore_ninja, 'host_identity', return_value=('macos', 'arm64')), \
                patch.object(arm.restore_ninja, 'binary_architectures', return_value={'arm64'}), \
                patch.object(arm.restore_ninja, 'probe_version', return_value='1.13.2'), \
                patch.object(arm.subprocess, 'run') as run:
            run.return_value.returncode = 1
            self.assertEqual(arm.compile_tree(ROOT, self.work, 1), 1)
            command = run.call_args.args[0]
            self.assertEqual(command[1:], ['-d', 'norebuild', '-C', str(self.work / 'src/out/Default'), '-j', '3', 'chrome'])
            self.assertFalse(any(key.startswith('DYLD_') for key in run.call_args.kwargs['env']))
            with self.assertRaises((ValueError, RuntimeError)):
                arm.compile_tree(ROOT, self.work, 2)

    def test_native_readonly_rejects_environment_or_tools_drift(self):
        old = json.loads(FIXTURE['files']['upstream-cache-preparation.json'])
        with patch.object(arm.prepared, 'host_identity', return_value=('macos', 'arm64')), \
                patch.object(arm.prepared, 'inspect_native_tools', return_value={'native_tools': True}), \
                patch.object(arm.prepared, 'environment_identity', return_value=old['environment']) as environment, \
                patch.object(arm.prepared, 'tool_fingerprint', return_value=old['tool_fingerprint']) as tools, \
                patch.object(arm.prepared, 'generator_fingerprint', side_effect=lambda *a: {
                    k: v for k, v in old['generator_fingerprint'].items() if k != 'macos154'}), \
                patch.object(arm.macos_restored_generators, 'prepare', return_value=old['generator_fingerprint']['macos154']) as generators, \
                patch.object(arm.macos_runtime, 'verify_runtime_loader'):
            arm.native_state(ROOT, self.work)
            self.assertNotIn('repair', generators.call_args.kwargs)
            tools.return_value = {}
            with self.assertRaises(ValueError):
                arm.native_state(ROOT, self.work)
            tools.return_value = old['tool_fingerprint']
            environment.return_value = {}
            with self.assertRaises(ValueError):
                arm.native_state(ROOT, self.work)


class WorkflowTest(unittest.TestCase):
    def test_fixed_manual_workflow_and_eight_isolated_handoffs(self):
        path = ROOT / '.github/workflows/build-mac156-arm64-stage8-resume.yml'
        workflow = yaml.safe_load(path.read_text())
        events = workflow.get('on', workflow.get(True))
        self.assertEqual(set(events), {'workflow_dispatch'})
        self.assertEqual(set(events['workflow_dispatch']['inputs']), {'compile_jobs'})
        inputs = workflow['jobs']['resume']['with']
        self.assertEqual((inputs['runner'], inputs['max-stages'], inputs['resume_tree_stage'], inputs['resume_attempt']),
                         ('macos-26', 8, '8', '1'))
        self.assertEqual(inputs['resume_artifact_ids'], str(arm.ARTIFACT))
        self.assertEqual(inputs['artifact'], 'chromix-mac156-arm64-stage8-resume')
        reusable = yaml.safe_load((ROOT / '.github/workflows/build-posix-github.yml').read_text())
        for number in range(1, 9):
            job = reusable['jobs'][f'posix-{number}']
            step = next(s for s in job['steps'] if s.get('id') == 'stage')
            mode = next(s for s in job['steps'] if s.get('name') == 'Enable fixed unchanged mac156 continuation')
            self.assertEqual(mode['if'], "inputs.resume_migration == 'mac156-arm64-stage8'")
            self.assertIn('CHROMIX_UNCHANGED_RESUME=mac156-arm64-stage8', mode['run'])
            final = next(s for s in job['steps'] if s.get('name') == 'Upload final bundle')
            self.assertIn("&& 'chromix-mac-arm64' || inputs.artifact", final['with']['path'])
            if number > 1:
                download = next(s for s in job['steps'] if s.get('name') == 'Download tree from previous stage')
                self.assertIn(f'needs.posix-{number - 1}.outputs.snapshot_attempt', download['with']['pattern'])
        first = reusable['jobs']['posix-1']['steps']
        guard = next(s for s in first if s.get('name') == 'Require exact mac156 ARM64 stage8 continuation inputs')['run']
        valid = {**inputs, 'github.repository': arm.REPOSITORY, 'github.event_name': 'workflow_dispatch'}
        def check(values):
            def replace(match):
                key = match[1]
                value = values.get(key, values.get(key.removeprefix('inputs.'), ''))
                return str(value).lower() if isinstance(value, bool) else str(value)
            script = re.sub(r'\$\{\{ (.*?) \}\}', replace, guard)
            return subprocess.run(['bash', '-c', script], capture_output=True).returncode
        self.assertEqual(check(valid), 0)
        for key, value in dict(platform='linux', arch='x64', runner='macos-26-intel', artifact='chromix-mac-arm64',
                               resume_run_id='1', resume_tree_stage='7', resume_attempt='2',
                               resume_artifact_ids='1', build_profile='release', use_upstream_cache=False,
                               **{'max-stages': 7}).items():
            self.assertNotEqual(check(dict(valid, **{key: value})), 0, key)

    def test_generated_yaml_and_runtime_gates_unchanged(self):
        path = ROOT / '.github/workflows/build-posix-github.yml'
        before = path.read_bytes()
        subprocess.run([sys.executable, 'tools/gen_posix_workflow.py'], cwd=ROOT, check=True, capture_output=True)
        self.assertEqual(before, path.read_bytes())
        stage = (ROOT / 'build/posix/ci-stage.sh').read_text()
        baseline = subprocess.check_output(['git', '-C', str(ROOT), 'show', 'e52742b:build/posix/ci-stage.sh']).decode()
        self.assertEqual(stage.split('# ---- completion checks')[1], baseline.split('# ---- completion checks')[1])
        self.assertLess(stage.index('unchanged_check before'), stage.index('# Cache opt-in'))
        self.assertIn('handoff() {\n  unchanged_check after', stage)
        subprocess.run(['bash', '-n', str(ROOT / 'build/posix/ci-stage.sh')], check=True)


if __name__ == '__main__':
    unittest.main()
