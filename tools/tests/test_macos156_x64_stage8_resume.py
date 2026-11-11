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

from tools import validate_macos156_x64_stage8_checkpoint as intel
from tools import validate_macos156_arm64_stage8_checkpoint as arm
from tools.tests.test_macos156_arm64_stage8_resume import real_ninja, sample_graph

from tools.tests.macos156_historical import recipe_repo

ROOT = Path(__file__).resolve().parents[2]
HISTORICAL = recipe_repo()
FIXTURE = json.loads(gzip.decompress(
    (ROOT / 'tools/tests/fixtures/macos156_x64_stage8_receipts.json.gz').read_bytes()))
ENV = {'GITHUB_RUN_ID': '999999', 'GITHUB_RUN_ATTEMPT': '1', 'CHROMIX_PREVIOUS_ATTEMPT': '1',
       'CHROMIX_BUILD_PROFILE': 'fast', 'CHROMIX_USE_UPSTREAM_CACHE': '1', 'CHROMIX_JOBS': '3'}


class Client:
    def __init__(self):
        self.data = copy.deepcopy(FIXTURE)

    def get(self, path):
        return {f'/actions/runs/{intel.RUN}': self.data['run'],
                f'/actions/jobs/{intel.JOB}': self.data['job']}[path]

    def items(self, path, key):
        assert path == f'/actions/runs/{intel.RUN}/artifacts' and key == 'artifacts'
        return [self.data['artifact']]


def metadata(client=None, raw=None):
    return intel.validate_metadata(client or Client(), intel.REPOSITORY, [intel.ARTIFACT],
                                 log_loader=lambda: raw if raw is not None else FIXTURE['producer_log'].encode())


def downloaded():
    manifest = intel.expected_manifest()
    artifact = dict(manifest['artifacts'][0], attempts=[{
        'status': 'success', 'size_in_bytes': intel.SIZE, 'sha256': intel.DIGEST.split(':')[1]}])
    return dict(status='success', phase='complete', publication='published',
                repository=intel.REPOSITORY, run_id=intel.RUN, head_sha=intel.SHA,
                artifacts=[artifact], total_size_in_bytes=8423029404,
                volumes=[dict(name='tree.tar.zst.001', artifact_id=intel.ARTIFACT,
                              size_in_bytes=8423029404, sha256='a' * 64)])


class MetadataTest(unittest.TestCase):
    def test_actual_stage8_budget_exhaustion_one_volume(self):
        self.assertEqual(metadata(), intel.expected_manifest())
        self.assertEqual(intel.sha(FIXTURE['producer_log'].encode()), intel.JOB_LOG_HASH)
        self.assertIn('packed 8423029404 bytes as 1 volumes', FIXTURE['producer_log'])

    def test_rejects_foreign_or_old_run_job_artifact(self):
        cases = {
            'run': dict(id=1, head_sha='a' * 40, head_branch='other', event='push', run_attempt=1,
                        status='in_progress', conclusion='success', path='old.yml',
                        repository={'full_name': 'foreign/repo'}, head_repository={'full_name': 'foreign/repo'}),
            'job': dict(id=1, run_id=1, head_sha='a' * 40, head_branch='other', run_attempt=1,
                        status='in_progress', conclusion='success', labels=['macos-26'], name='stage 7'),
            'artifact': dict(id=1, name='chromix-mac-x64-tree-s8-attempt-1-part2',
                             size_in_bytes=intel.SIZE - 1, digest='sha256:' + 'a' * 64, expired=True),
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

    def test_arm_identity_and_attempt_cannot_satisfy_intel_metadata(self):
        from tools.tests.test_macos156_arm64_stage8_resume import FIXTURE as arm_fixture
        for field in ('run', 'job', 'artifact'):
            client = Client()
            client.data[field] = copy.deepcopy(arm_fixture[field])
            with self.subTest(field=field), self.assertRaises(ValueError):
                metadata(client)
        for repository, artifacts in [('other/repo', [intel.ARTIFACT]),
                                      (intel.REPOSITORY, [arm.ARTIFACT])]:
            with self.assertRaises(ValueError):
                intel.validate_metadata(Client(), repository, artifacts)
        self.assertNotEqual(intel.UPSTREAM_IDENTITY, arm.UPSTREAM_IDENTITY)
        self.assertNotEqual(intel.PREPARATION_HASH, arm.PREPARATION_HASH)
        for subject in ('run', 'job'):
            client = Client()
            client.data[subject]['run_attempt'] = '2'
            with self.assertRaises(ValueError):
                metadata(client)

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
        subprocess.run(['git', '-C', str(cls.donor), 'checkout', '--quiet', intel.SHA], check=True)

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
            'src/.chromix-source-ready': intel.KEY + '\n', 'src/.chromix-toolchain-ready': '',
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
        self.origin.write_text(json.dumps(intel.expected_manifest()))
        self.download.write_text(json.dumps(downloaded()))
        self.enterContext(patch.dict(os.environ, ENV))

    def admit(self):
        with patch.object(intel, 'native_state'), patch.object(intel, 'live_source'), \
                patch.object(intel.migration, '_verify_tooling'), \
                patch.object(intel.migration.restore, 'verify_restored'):
            return intel.admit(self.work, self.donor, HISTORICAL, self.origin, self.download)

    def test_real_preimages_and_selected_226_patch_recipe(self):
        for name, digest in intel.PINNED_FILES.items():
            self.assertEqual(intel.sha((self.work / name).read_bytes()), digest, name)
        source = json.loads(FIXTURE['files']['fingerprint-diagnostics/source-final.json'])
        self.assertEqual(source['patch_count'], 226)
        self.assertEqual(intel.identity_hash(source['identity']), intel.SOURCE_IDENTITY)
        self.assertEqual(intel.identity_hash(source['outputs']), intel.SOURCE_OUTPUTS)
        self.assertEqual(intel.recipes(HISTORICAL, self.donor), intel.recipes(HISTORICAL))
        self.assertEqual(intel.migration.source_ready_key(self.donor, 'macos', 'x64'), intel.KEY)
        self.assertEqual(intel.arp.patch_selection.select(HISTORICAL, 'macos'),
                         intel.arp.patch_selection.select(self.donor, 'macos'))

    def test_reviewed_linux_only_function_exception_not_source_sha_trust(self):
        name = 'tools/prepare_restored_build.py'
        old = (self.donor / name).read_bytes()
        new = (HISTORICAL / name).read_bytes()
        self.assertNotEqual(old, new)
        self.assertEqual(intel.recipe_bytes(name, old), intel.recipe_bytes(name, new))
        for bad in (new + b'\n', new.replace(b'def environment_identity', b'def different_identity')):
            with self.assertRaises(ValueError):
                intel.recipe_bytes(name, bad)

    def test_download_manifest_size_digest_attempt_and_single_volume(self):
        intel.download_identity(self.origin, self.download)
        for field, value in dict(attempt=1, stage=7, run_id=1, job_id=1, head_sha='a' * 40).items():
            altered = intel.expected_manifest()
            altered[field] = value
            self.origin.write_text(json.dumps(altered))
            with self.assertRaises(ValueError):
                intel.download_identity(self.origin, self.download)
        self.origin.write_text(json.dumps(intel.expected_manifest()))
        cases = [dict(status='verified'), dict(publication='unconfirmed'), dict(phase='extract'),
                 dict(total_size_in_bytes=1), dict(volumes=[]), dict(run_id=1)]
        for change in cases:
            self.download.write_text(json.dumps(dict(downloaded(), **change)))
            with self.assertRaises(ValueError):
                intel.download_identity(self.origin, self.download)
        for key, value in [('size_in_bytes', 1), ('sha256', 'f' * 64), ('status', 'failed')]:
            data = downloaded()
            data['artifacts'][0]['attempts'][-1][key] = value
            self.download.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                intel.download_identity(self.origin, self.download)

    def test_admission_changes_no_source_gn_output_or_preparation(self):
        before = intel.output_inventory(self.work / 'src')
        result = self.admit()
        self.assertEqual(before, intel.output_inventory(self.work / 'src'))
        self.assertEqual(result['changed_files'], [])
        self.assertEqual(result['artifact_size'], intel.SIZE)
        self.assertEqual(result['provenance']['manifest'], intel.expected_manifest())
        with self.assertRaises(ValueError):
            self.admit()

    def test_receipts_markers_and_gn_preimage_fail_closed(self):
        for name in (*intel.PINNED_FILES, 'src/.chromix-restored-build-prepared.json',
                     'src/.chromix-source-ready', 'src/chrome/VERSION'):
            path = self.work / name
            original = path.read_bytes()
            path.write_bytes(original + b'changed')
            with self.subTest(name=name), self.assertRaises((ValueError, RuntimeError)):
                intel.source_state(HISTORICAL, self.work)
            path.write_bytes(original)
        for name in (intel.migration.TRANSACTION, intel.arp.IN_PROGRESS):
            path = self.work / 'src' / name
            path.touch()
            with self.assertRaises(ValueError):
                intel.source_state(HISTORICAL, self.work)
            path.unlink()
        target = self.work / 'src/out/Default/args.gn'
        target.unlink()
        target.symlink_to(self.work / 'src/.chromix-source-ready')
        with self.assertRaises(ValueError):
            intel.source_state(HISTORICAL, self.work)

    def test_continuation_isolated_stages_and_graph_immutable(self):
        self.admit()
        with patch.object(intel, 'native_state'), patch.object(intel, 'live_source'):
            for stage in range(1, 9):
                before = intel.continuation(HISTORICAL, self.work, stage, 'before')
                self.assertEqual(before['logical_stage'], stage + 8)
                intel.continuation(HISTORICAL, self.work, stage, 'after')
            for stage in (0, 9):
                with self.assertRaises(ValueError):
                    intel.continuation(HISTORICAL, self.work, stage, 'before')
            with patch.dict(os.environ, GITHUB_RUN_ID='888888'):
                with self.assertRaises(ValueError):
                    intel.continuation(HISTORICAL, self.work, 2, 'before')
            with patch.dict(os.environ, CHROMIX_USE_UPSTREAM_CACHE='0'):
                with self.assertRaises(ValueError):
                    intel.continuation(HISTORICAL, self.work, 2, 'before')
            path = self.work / 'src/out/Default/obj/test.ninja'
            path.write_text('changed graph')
            with self.assertRaises(ValueError):
                intel.continuation(HISTORICAL, self.work, 2, 'before')

    def test_live_source_rejects_old_count_identity_or_output_hash(self):
        original = json.loads(FIXTURE['files']['fingerprint-diagnostics/source-final.json'])
        for index, changes in enumerate(({}, {'patch_count': 224}, {'status': 'failed'},
                                          {'identity': {}}, {'outputs': {}}, {'domain_substituted': False})):
            output = self.work / f'live-{index}.json'
            def verify(*args, **kwargs):
                output.write_text(json.dumps(dict(original, **changes)))
            with patch.object(intel.subprocess, 'run', side_effect=verify):
                if changes:
                    with self.assertRaises(ValueError):
                        intel.live_source(HISTORICAL, self.work, output)
                else:
                    intel.live_source(HISTORICAL, self.work, output)

    def test_missing_handoff_and_same_run_changed_state_rejected(self):
        self.admit()
        with patch.object(intel, 'native_state'), patch.object(intel, 'live_source'):
            with self.assertRaises(ValueError):
                intel.continuation(HISTORICAL, self.work, 2, 'before')
            intel.continuation(HISTORICAL, self.work, 1, 'before')
            intel.continuation(HISTORICAL, self.work, 1, 'after')
            (self.work / 'src/out/Default/.ninja_log').write_text('# ninja log v7\nchanged')
            with self.assertRaises(ValueError):
                intel.continuation(HISTORICAL, self.work, 2, 'before')

    def test_compile_real_ninja_with_fresh_and_unchanged_receipts(self):
        real_ninja(self)
        if intel.restore_ninja.host_identity() != ('macos', 'x64'):
            self.skipTest('receipt compile requires native macOS Intel')
        out = self.work / 'src/out/Default'
        original_args = (out / "args.gn").read_bytes()
        sample_graph(out)
        (out / "args.gn").write_bytes(original_args)
        self.admit()
        with patch.object(intel, 'native_state'), patch.object(intel, 'live_source'):
            intel.continuation(HISTORICAL, self.work, 1, 'before')
        proof = intel.source_state(HISTORICAL, self.work)
        self.assertEqual(set(proof), {'out/Default/build.ninja', 'out/Default/.ninja_log',
                                     'out/Default/.ninja_deps', 'graph'})
        self.assertEqual(intel.compile_tree(HISTORICAL, self.work, 1), 0)
        self.assertTrue((out / 'sample.o').is_file())
        self.assertFalse((out / 'REGENERATED').exists())
        self.assertEqual(intel.source_state(HISTORICAL, self.work)['graph'], proof['graph'])
        unchanged = intel.output_inventory(out)
        with patch.object(intel, 'native_state'), patch.object(intel, 'live_source'):
            intel.continuation(HISTORICAL, self.work, 1, 'after')
            intel.continuation(HISTORICAL, self.work, 2, 'before')
        self.assertEqual(intel.compile_tree(HISTORICAL, self.work, 2), 0)
        self.assertEqual(intel.output_inventory(out)['sample.o'], unchanged['sample.o'])
        with patch.dict(os.environ, GITHUB_RUN_ATTEMPT='2'):
            with self.assertRaises((ValueError, RuntimeError)):
                intel.compile_tree(HISTORICAL, self.work, 2)
        (out / 'build.ninja').write_text((out / 'build.ninja').read_text() + '# changed\n')
        with self.assertRaises(ValueError):
            intel.compile_tree(HISTORICAL, self.work, 2)

    def test_intel_compile_rejects_foreign_host_ninja_and_invalid_jobs(self):
        self.admit()
        with patch.object(intel, 'native_state'), patch.object(intel, 'live_source'):
            intel.continuation(HISTORICAL, self.work, 1, 'before')
        with patch.object(intel.restore_ninja, 'host_identity', return_value=('macos', 'x64')) as host, \
                patch.object(intel.restore_ninja, 'binary_architectures', return_value={'x64'}) as arches, \
                patch.object(intel.restore_ninja, 'probe_version', return_value='1.13.2') as version, \
                patch.object(intel, 'frozen_ninja', return_value=0) as compile:
            for probe, value, original in [(host, ('macos', 'arm64'), ('macos', 'x64')),
                                            (host, ('linux', 'x64'), ('macos', 'x64')),
                                            (arches, {'arm64'}, {'x64'}),
                                            (arches, {'x64', 'arm64'}, {'x64'}),
                                            (version, '1.12.1', '1.13.2')]:
                probe.return_value = value
                with self.assertRaises(ValueError):
                    intel.compile_tree(HISTORICAL, self.work, 1)
                probe.return_value = original
            for jobs in ('0', '-1', 'auto', '2; echo unsafe'):
                with patch.dict(os.environ, CHROMIX_JOBS=jobs), self.assertRaises(ValueError):
                    intel.compile_tree(HISTORICAL, self.work, 1)
            compile.assert_not_called()
            with patch.dict(os.environ, DYLD_LIBRARY_PATH='/foreign'):
                self.assertEqual(intel.compile_tree(HISTORICAL, self.work, 1), 0)
            self.assertNotIn('DYLD_LIBRARY_PATH', compile.call_args.args[-1])
        with patch.object(intel.prepared, 'host_identity', return_value=('macos', 'arm64')):
            with self.assertRaisesRegex(ValueError, 'native macOS Intel'):
                intel.native_state(HISTORICAL, self.work)

    def test_intel_entry_reuses_current_real_ninja_isolation(self):
        self.assertIs(intel.frozen_ninja, arm.frozen_ninja)
        ninja = real_ninja(self)
        out = self.work / 'src/out/Default'
        (out / 'obj/test.ninja').unlink()
        sample_graph(out)
        original = intel.output_inventory(out)
        self.assertEqual(intel.frozen_ninja(ninja, self.work, '2', dict(os.environ)), 0)
        self.assertFalse((out / 'REGENERATED').exists())
        self.assertTrue((out / 'sample.o').is_file())
        for name in ('build.ninja', 'args.gn', 'sample.c'):
            self.assertEqual(intel.output_inventory(out)[name], original[name])
        sample_graph(out, ' |@ build.ninja')
        original = intel.output_inventory(out)
        with self.assertRaisesRegex(ValueError, 'regeneration.*forbidden'):
            intel.frozen_ninja(ninja, self.work, '2', dict(os.environ))
        self.assertEqual(intel.output_inventory(out), original)

    def test_native_readonly_rejects_environment_or_tools_drift(self):
        old = json.loads(FIXTURE['files']['upstream-cache-preparation.json'])
        with patch.object(intel.prepared, 'host_identity', return_value=('macos', 'x64')), \
                patch.object(intel.prepared, 'inspect_native_tools', return_value={'native_tools': True}), \
                patch.object(intel.prepared, 'environment_identity', return_value=old['environment']) as environment, \
                patch.object(intel.prepared, 'tool_fingerprint', return_value=old['tool_fingerprint']) as tools, \
                patch.object(intel.prepared, 'generator_fingerprint', side_effect=lambda *a: {
                    k: v for k, v in old['generator_fingerprint'].items() if k != 'macos154'}), \
                patch.object(intel.macos_restored_generators, 'prepare', return_value=old['generator_fingerprint']['macos154']) as generators, \
                patch.object(intel.macos_runtime, 'verify_runtime_loader'):
            intel.native_state(HISTORICAL, self.work)
            self.assertNotIn('repair', generators.call_args.kwargs)
            tools.return_value = {}
            with self.assertRaises(ValueError):
                intel.native_state(HISTORICAL, self.work)
            tools.return_value = old['tool_fingerprint']
            environment.return_value = {}
            with self.assertRaises(ValueError):
                intel.native_state(HISTORICAL, self.work)


class WorkflowTest(unittest.TestCase):
    def test_fixed_manual_workflow_and_eight_isolated_handoffs(self):
        path = ROOT / '.github/workflows/build-mac156-x64-stage8-resume.yml'
        workflow = yaml.safe_load(path.read_text())
        events = workflow.get('on', workflow.get(True))
        self.assertEqual(set(events), {'workflow_dispatch'})
        self.assertEqual(set(events['workflow_dispatch']['inputs']), {'compile_jobs'})
        inputs = workflow['jobs']['resume']['with']
        self.assertEqual((inputs['runner'], inputs['max-stages'], inputs['resume_tree_stage'], inputs['resume_attempt']),
                         ('macos-26-intel', 8, '8', '2'))
        self.assertEqual(inputs['resume_artifact_ids'], str(intel.ARTIFACT))
        self.assertEqual(inputs['artifact'], 'chromix-mac156-x64-stage8-resume')
        reusable = yaml.safe_load((ROOT / '.github/workflows/build-posix-github.yml').read_text())
        for number in range(1, 9):
            job = reusable['jobs'][f'posix-{number}']
            step = next(s for s in job['steps'] if s.get('id') == 'stage')
            mode = next(s for s in job['steps'] if s.get('name') == 'Enable fixed unchanged mac156 Intel continuation')
            self.assertEqual(mode['if'], "inputs.resume_migration == 'mac156-x64-stage8'")
            self.assertIn('CHROMIX_UNCHANGED_RESUME=mac156-x64-stage8', mode['run'])
            final = next(s for s in job['steps'] if s.get('name') == 'Upload final bundle')
            self.assertIn("&& 'chromix-mac-x64' || inputs.artifact", final['with']['path'])
            if number > 1:
                download = next(s for s in job['steps'] if s.get('name') == 'Download tree from previous stage')
                self.assertIn(f'needs.posix-{number - 1}.outputs.snapshot_attempt', download['with']['pattern'])
        first = reusable['jobs']['posix-1']['steps']
        guard = next(s for s in first if s.get('name') == 'Require exact mac156 Intel stage8 continuation inputs')['run']
        valid = {**inputs, 'github.repository': intel.REPOSITORY, 'github.event_name': 'workflow_dispatch'}
        def check(values):
            def replace(match):
                key = match[1]
                value = values.get(key, values.get(key.removeprefix('inputs.'), ''))
                return str(value).lower() if isinstance(value, bool) else str(value)
            script = re.sub(r'\$\{\{ (.*?) \}\}', replace, guard)
            return subprocess.run(['bash', '-c', script], capture_output=True).returncode
        self.assertEqual(check(valid), 0)
        for key, value in dict(platform='linux', arch='arm64', runner='macos-26', artifact='chromix-mac-x64',
                               resume_run_id='1', resume_tree_stage='7', resume_attempt='1',
                               resume_artifact_ids='1', build_profile='release', use_upstream_cache=False,
                               **{'max-stages': 7}).items():
            self.assertNotEqual(check(dict(valid, **{key: value})), 0, key)

    def test_failed_tree_preservation_and_verified_upload_in_every_stage(self):
        reusable = yaml.safe_load((ROOT / '.github/workflows/build-posix-github.yml').read_text())
        for number in range(1, 9):
            steps = reusable['jobs'][f'posix-{number}']['steps']
            preserve = next(s for s in steps if s.get('id') == 'runtime_checkpoint')
            self.assertIn("inputs.resume_migration == 'mac156-x64-stage8' && failure()", preserve['if'])
            self.assertIn('test -d "$WORK/src"', preserve['run'])
            self.assertIn("steps.stage.outputs.upload_snapshot", preserve['run'])
            verify = next(s for s in steps if s.get('id') == 'checkpoint')
            self.assertIn('steps.runtime_checkpoint.outputs.upload_snapshot', verify['if'])
            self.assertIn('verify_snapshot_tar.py', verify['run'])
            for part in range(1, 5):
                upload = next(s for s in steps if s.get('name') == f'Upload tree part {part}')
                self.assertIn("steps.checkpoint.outcome == 'success'", upload['if'])
                self.assertIn('steps.runtime_checkpoint.outputs.upload_snapshot', upload['if'])
                self.assertIn(f'-tree-s{number}-attempt-', upload['with']['name'])

    def test_shell_rejects_cold_fallback_unknown_mode_and_foreign_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            def run(mode, arch='x64'):
                return subprocess.run(['bash', str(ROOT / 'build/posix/ci-stage.sh'),
                                       '--platform', 'macos', '--arch', arch, '--workdir', str(work)],
                                      capture_output=True, text=True, timeout=30,
                                      env={**os.environ, **ENV, 'CHROMIX_UNCHANGED_RESUME': mode})
            for mode, arch in [('mac156-x64-stage8', 'x64'),
                               ('mac156-x64-stage8', 'arm64'), ('unknown', 'x64')]:
                self.assertNotEqual(run(mode, arch).returncode, 0)
                self.assertFalse((work / 'src').exists())
            path = work / intel.RECEIPT
            path.parent.mkdir()
            path.write_text('{}')
            result = run('')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('requires the dedicated continuation mode', result.stderr)
            self.assertFalse((work / 'src').exists())

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
