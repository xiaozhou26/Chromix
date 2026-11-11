"""Exact failed Intel stage3 evidence and read-only stage handoff lifecycle."""
import copy
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from tools import validate_macos156_x64_stage3_checkpoint as latest
from tools import validate_macos156_x64_stage8_checkpoint as intel
from tools import validate_macos156_x64_stage2_checkpoint as previous
from tools import validate_macos156_arm64_stage8_checkpoint as arm
from tools.tests import test_macos156_x64_stage8_resume as intel_tests
from tools.tests.test_macos156_arm64_stage8_resume import real_ninja, sample_graph

from tools.tests.macos156_historical import recipe_repo

ROOT = Path(__file__).resolve().parents[2]
HISTORICAL = recipe_repo()
FIXTURE = json.loads(gzip.decompress(
    (ROOT / 'tools/tests/fixtures/macos156_x64_stage3_receipts.json.gz').read_bytes()))
LIVE_SOURCE = intel.live_source


def emit_source(repo, work, output):
    original = intel.subprocess.run

    def run(command, *args, **kwargs):
        if 'verify_patch_stack.py' in command[1]:
            output.write_bytes((work / 'fingerprint-diagnostics/source-final.json').read_bytes())
            return subprocess.CompletedProcess(command, 0)
        return original(command, *args, **kwargs)

    with patch.object(intel.subprocess, 'run', side_effect=run):
        LIVE_SOURCE(repo, work, output)


class Client:
    def __init__(self):
        self.data = copy.deepcopy(FIXTURE)

    def get(self, path):
        return {f'/actions/runs/{latest.RUN}': self.data['run'],
                f'/actions/jobs/{latest.JOB}': self.data['job']}[path]

    def items(self, path, key):
        assert path == f'/actions/runs/{latest.RUN}/artifacts' and key == 'artifacts'
        return self.data['artifacts']


def metadata(client=None, raw=None):
    return latest.validate_metadata(client or Client(), intel.REPOSITORY, [latest.ARTIFACTS[0]['id']],
                                    log_loader=lambda: FIXTURE['producer_log'].encode() if raw is None else raw)


def downloaded():
    artifact = latest.ARTIFACTS[0]
    return dict(status='success', publication='published', phase='complete', repository=intel.REPOSITORY,
                run_id=latest.RUN, head_sha=latest.SHA, total_size_in_bytes=sum(latest.VOLUME_SIZES),
                artifacts=[dict(artifact, attempts=[dict(status='success', size_in_bytes=artifact['size_in_bytes'],
                                                        sha256=artifact['digest'].split(':')[1])])],
                volumes=[dict(name='tree.tar.zst.001', artifact_id=artifact['id'],
                              size_in_bytes=latest.VOLUME_SIZES[0], sha256='a' * 64)])


class MetadataTest(unittest.TestCase):
    def test_real_failure_is_sdk_timeout_before_ninja_not_compiler_error(self):
        self.assertEqual(metadata(), latest.expected_manifest())
        self.assertEqual(latest.expected_manifest()['stage'], 3)
        self.assertIn("'--show-sdk-build-version')' timed out after 30 seconds", FIXTURE['producer_log'])
        sdk_step = next(s for s in FIXTURE['job']['steps'] if s['name'] == 'Verify complete Mac SDK contents')
        self.assertEqual(sdk_step['conclusion'], 'success')
        self.assertIn('86700d19c5fc9971a1feef8ce5747fca5b1d3bcccd9a3d2ab7412a152c3bb1e7',
                      FIXTURE['producer_log'])
        self.assertNotIn('FAILED: obj/', FIXTURE['producer_log'])
        for name, text in FIXTURE['files'].items():
            self.assertEqual(intel.sha(text.encode()), latest.DONOR_RECEIPTS[name])
        for part in (2, 3, 4):
            self.assertIn(f'.snapshot-stage-3/p{part}/. No artifacts will be uploaded.', FIXTURE['producer_log'])

    def test_wrong_run_job_and_upload_evidence_fail_closed(self):
        cases = {
            'run': dict(id=1, head_sha=intel.SHA, head_branch='other', event='push', run_attempt='1',
                        status='in_progress', conclusion='success', path='other.yml', repository={}, head_repository={}),
            'job': dict(id=1, run_id=1, head_sha=intel.SHA, head_branch='other', run_attempt=2,
                        status='in_progress', conclusion='success', labels=['macos-26'], name='stage 1'),
        }
        for subject, changes in cases.items():
            for key, value in changes.items():
                client = Client()
                client.data[subject][key] = value
                with self.subTest(subject=subject, key=key), self.assertRaises(ValueError):
                    metadata(client)
        for name in ('Run stage 3', 'Verify handoff snapshot', 'Preserve latest macOS compiled checkpoint',
                     'Select compatible Xcode', 'Verify complete Mac SDK contents',
                     *(f'Upload tree part {i}' for i in range(1, 5))):
            client = Client()
            next(s for s in client.data['job']['steps'] if s['name'] == name)['conclusion'] = 'skipped'
            with self.assertRaises(ValueError):
                metadata(client)
        for raw in (b'', FIXTURE['producer_log'].encode() + b'\n'):
            with self.assertRaises(ValueError):
                metadata(raw=raw)
        for repository, ids in [('foreign/repo', [latest.ARTIFACTS[0]['id']]),
                                (intel.REPOSITORY, [intel.ARTIFACT])]:
            with self.assertRaises(ValueError):
                latest.validate_metadata(Client(), repository, ids)

    def test_artifact_digest_origin_and_complete_set(self):
        for key, value in dict(id=1, name=latest.PREFIX + '2', size_in_bytes=1, expired=True,
                               digest='sha256:' + 'f' * 64, workflow_run={}).items():
            client = Client()
            artifact = next(a for a in client.data['artifacts'] if a['name'] == latest.PREFIX + '1')
            artifact[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                metadata(client)
        for artifacts in ([], [dict(latest.ARTIFACTS[0])] * 2):
            client = Client()
            client.data['artifacts'] = artifacts
            with self.assertRaises(ValueError):
                metadata(client)

    def test_exact_download_manifest_zip_digest_and_volume(self):
        with tempfile.TemporaryDirectory() as temporary:
            origin, report = Path(temporary) / 'origin', Path(temporary) / 'download'
            origin.write_text(json.dumps(latest.expected_manifest()))
            report.write_text(json.dumps(downloaded()))
            latest.download_identity(origin, report)
            for key, value in dict(run_id=previous.RUN, stage=2, attempt=2, job_id=1,
                                   head_sha=intel.SHA, arch='arm64').items():
                origin.write_text(json.dumps(dict(latest.expected_manifest(), **{key: value})))
                with self.assertRaises(ValueError):
                    latest.download_identity(origin, report)
            origin.write_text(json.dumps(latest.expected_manifest()))
            for changes in (dict(publication='pending'), dict(phase='extract'), dict(status='failed'),
                            dict(run_id=intel.RUN), dict(total_size_in_bytes=1), dict(volumes=[]), dict(artifacts=[])):
                report.write_text(json.dumps(dict(downloaded(), **changes)))
                with self.assertRaises(ValueError):
                    latest.download_identity(origin, report)
            for subject, keys in [('attempt', ('status', 'size_in_bytes', 'sha256')),
                                  ('volume', ('name', 'artifact_id', 'size_in_bytes', 'sha256'))]:
                for key in keys:
                    data = downloaded()
                    target = data['volumes'][0] if subject == 'volume' else data['artifacts'][0]['attempts'][-1]
                    target[key] = 'wrong'
                    report.write_text(json.dumps(data))
                    with self.assertRaises(ValueError):
                        latest.download_identity(origin, report)


class AdmissionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checkout = tempfile.TemporaryDirectory()
        cls.donor = Path(cls.checkout.name) / 'donor'
        subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout', str(ROOT), str(cls.donor)], check=True)
        subprocess.run(['git', '-C', str(cls.donor), 'checkout', '--quiet', latest.SHA], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.checkout.cleanup()

    def setUp(self):
        intel_tests.ReceiptTest.setUp(self)
        for name, text in FIXTURE['files'].items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.origin.write_text(json.dumps(latest.expected_manifest()))
        self.download.write_text(json.dumps(downloaded()))
        self.old, self.handoff = latest.donor_identity(self.work)

    def admit(self):
        with patch.object(intel, 'native_state'), patch.object(intel, 'live_source', side_effect=emit_source), \
                patch.object(intel.migration.restore, 'verify_restored'), patch.object(intel.migration, '_verify_tooling'):
            return latest.admit(self.work, self.donor, HISTORICAL, self.origin, self.download)

    def test_real_receipt_state_differs_from_handoff_no_completed_stage3(self):
        self.assertNotEqual(self.old['state'], self.handoff['state'])
        self.assertEqual(self.old['state']['graph'], self.handoff['state']['graph'])
        self.assertEqual(self.handoff['logical_stage'], 11)
        self.assertEqual(intel.recipes(HISTORICAL), self.old['recipes'])
        with patch.object(intel, 'source_state', return_value=self.old['state']), self.assertRaises(ValueError):
            self.admit()
        with patch.object(intel, 'source_state', return_value=self.handoff['state']):
            inventory = intel.output_inventory(self.work / 'src')
            result = self.admit()
            self.assertEqual(result['state'], self.handoff['state'])
            self.assertEqual(result['completed_logical_stage'], 11)
            self.assertEqual(result['changed_files'], [])
            self.assertEqual(inventory, intel.output_inventory(self.work / 'src'))
            with self.assertRaises(ValueError):
                self.admit()

    def test_donor_receipts_missing_altered_duplicate_or_fake_stage3_rejected(self):
        for name in latest.DONOR_RECEIPTS:
            path = self.work / name
            original = path.read_bytes()
            path.unlink()
            with self.assertRaises((ValueError, OSError)):
                latest.donor_identity(self.work)
            path.write_bytes(original + b' ')
            with self.assertRaises(ValueError):
                latest.donor_identity(self.work)
            path.write_bytes(original)
        for name in ('mac156-x64-stage2-continuation-s2-after-attempt-2.json',
                     'mac156-x64-stage2-continuation-s3-before-attempt-1.json',
                     'mac156-x64-stage2-continuation-s3-after-attempt-1-source.json'):
            fake = self.work / 'fingerprint-diagnostics' / name
            fake.write_text(json.dumps(self.handoff))
            with self.assertRaises(ValueError):
                latest.donor_identity(self.work)
            fake.unlink()
        previous.donor_identity(self.work)
        self.assertEqual(previous.RUN, 42683707985)
        self.assertEqual(previous.expected_manifest()['stage'], 2)


    def test_admission_rejects_dirty_checkout_recipe_and_graph_changes(self):
        dirty = self.donor / 'unexpected-file'
        try:
            dirty.write_text('dirty donor')
            with self.assertRaisesRegex(ValueError, 'clean exact'):
                self.admit()
        finally:
            dirty.unlink()
        with patch.object(intel, 'source_state', return_value=self.handoff['state']):
            with patch.object(intel, 'recipes', return_value={}), self.assertRaisesRegex(ValueError, 'recipes'):
                self.admit()
            with patch.object(intel, 'recipes', return_value=self.old['recipes']), \
                    patch.object(intel.arp.patch_selection, 'select', side_effect=[({}, []), ({}, [('changed', b'')])]), \
                    self.assertRaisesRegex(ValueError, 'patches'):
                self.admit()
        wrong = dict(self.handoff['state'], graph='f' * 64)
        with patch.object(intel, 'source_state', return_value=wrong), self.assertRaisesRegex(ValueError, 'Ninja state/graph'):
            self.admit()

    def test_native_timeout_and_wrong_sdk_fail_closed_without_retries(self):
        with patch.object(intel, 'source_state', return_value=self.handoff['state']):
            self.admit()
            for error in (subprocess.TimeoutExpired(('xcrun', '--sdk', 'macosx',
                                                      '--show-sdk-build-version'), 30),
                          ValueError('SDK/toolchain environment changed; no prepare fallback')):
                with patch.object(intel, 'native_state', side_effect=error) as native, \
                        patch.object(intel, 'live_source') as source:
                    with self.assertRaises(type(error)):
                        latest.continuation(HISTORICAL, self.work, 1, 'before')
                    native.assert_called_once_with(HISTORICAL, self.work)
                    source.assert_not_called()
                    self.assertFalse((self.work / 'fingerprint-diagnostics' /
                                      f'{latest.CONTINUATION}-s1-before-attempt-1.json').exists())

    def test_continuation_admission_provenance_environment_and_initial_state(self):
        state = self.handoff['state']
        with patch.object(intel, 'source_state', return_value=state) as source, \
                patch.object(intel, 'native_state'), patch.object(intel, 'live_source', side_effect=emit_source):
            receipt = self.admit()
            path = self.work / latest.RECEIPT
            for key, value in dict(run_id=intel.RUN, attempt=2, stage=1, job_id=1, arch='arm64',
                                   head_sha=intel.SHA, artifacts=[], completed_logical_stage=10,
                                   continuation_run='1', provenance={}, recipes={}).items():
                path.write_text(json.dumps(dict(receipt, **{key: value})))
                with self.subTest(key=key), self.assertRaises(ValueError):
                    latest.continuation(HISTORICAL, self.work, 1, 'before')
            path.write_text(json.dumps(receipt))
            for key, value in dict(GITHUB_RUN_ID='0', GITHUB_RUN_ATTEMPT='', CHROMIX_BUILD_PROFILE='release',
                                   CHROMIX_USE_UPSTREAM_CACHE='0').items():
                with patch.dict(os.environ, {key: value}), self.assertRaises(ValueError):
                    latest.continuation(HISTORICAL, self.work, 1, 'before')
            for stage in (0, 9):
                with self.assertRaises(ValueError):
                    latest.continuation(HISTORICAL, self.work, stage, 'before')
            source.return_value = dict(state, **{'out/Default/.ninja_log': 'wrong'})
            with self.assertRaisesRegex(ValueError, 'initial Ninja state'):
                latest.continuation(HISTORICAL, self.work, 1, 'before')
            source.return_value = dict(state, graph='wrong')
            with self.assertRaisesRegex(ValueError, 'GN graph changed'):
                latest.continuation(HISTORICAL, self.work, 1, 'before')

    def test_new_handoff_lifecycle_pack_restore_negative_and_rerun_attempt(self):
        state = self.handoff['state']
        with patch.object(intel, 'source_state', return_value=state) as source, \
                patch.object(intel, 'native_state'), patch.object(intel, 'live_source', side_effect=emit_source):
            self.admit()
            first = latest.continuation(HISTORICAL, self.work, 1, 'before')
            self.assertEqual(first['logical_stage'], 12)
            source.return_value = dict(state, **{'out/Default/.ninja_log': 'b' * 64})
            after = latest.continuation(HISTORICAL, self.work, 1, 'after')
            name = f'fingerprint-diagnostics/{latest.CONTINUATION}-s1-after-attempt-1.json'
            path = self.work / name
            self.assertTrue(path.with_name(path.stem + '-source.json').exists())
            for key, value in dict(phase='before', continuation_stage=2, logical_stage=11, completed_logical_stage=10,
                                   continuation_run='1', continuation_attempt='2', arch='arm64', run_id=intel.RUN,
                                   attempt=2, stage=1, job_id=1, head_sha=intel.SHA, tree=intel.TREE,
                                   artifacts=[], version='155', preparation_key='wrong', source_identity='wrong',
                                   producer_log_sha256='wrong', state={}).items():
                path.write_text(json.dumps(dict(after, **{key: value})))
                with self.subTest(key=key), self.assertRaises(ValueError):
                    latest.continuation(HISTORICAL, self.work, 2, 'before')
            path.write_text(json.dumps(after))
            for previous in ('', '0', '2'):
                with patch.dict(os.environ, CHROMIX_PREVIOUS_ATTEMPT=previous), self.assertRaises(ValueError):
                    latest.continuation(HISTORICAL, self.work, 2, 'before')
            duplicate = path.with_name(path.name.replace('attempt-1', 'attempt-2'))
            duplicate.write_bytes(path.read_bytes())
            with self.assertRaises(ValueError):
                latest.continuation(HISTORICAL, self.work, 2, 'before')
            duplicate.unlink()
            parts, restored = self.work.parent / (self.work.name + '-parts'), self.work.parent / (self.work.name + '-restored')
            self.addCleanup(shutil.rmtree, parts, True)
            self.addCleanup(shutil.rmtree, restored, True)
            subprocess.run(['/bin/bash', str(ROOT / 'build/posix/ci-parts.sh'), str(self.work), str(parts)],
                           check=True, capture_output=True, env=dict(os.environ, COPYFILE_DISABLE='1'))
            subprocess.run([sys.executable, str(ROOT / 'tools/restore_posix_snapshot.py'), str(parts), str(restored)],
                           check=True, capture_output=True)
            with patch.dict(os.environ, GITHUB_RUN_ATTEMPT='2', CHROMIX_PREVIOUS_ATTEMPT='1'):
                self.assertEqual(latest.continuation(HISTORICAL, restored, 2, 'before')['logical_stage'], 13)
                latest.continuation(HISTORICAL, restored, 2, 'after')
            for name in latest.DONOR_RECEIPTS:
                self.assertEqual((self.work / name).read_bytes(), (restored / name).read_bytes())
            with patch.dict(os.environ, GITHUB_RUN_ATTEMPT='2', CHROMIX_PREVIOUS_ATTEMPT='2'):
                latest.continuation(HISTORICAL, restored, 3, 'before')

    def test_compile_gate_full_identity_native_ninja_and_no_dyld(self):
        with patch.object(intel, 'source_state', return_value=self.handoff['state']), \
                patch.object(intel, 'native_state'), patch.object(intel, 'live_source', side_effect=emit_source):
            self.admit()
            before = latest.continuation(HISTORICAL, self.work, 1, 'before')
            path = self.work / f'fingerprint-diagnostics/{latest.CONTINUATION}-s1-before-attempt-1.json'
            with patch.object(intel.restore_ninja, 'host_identity', return_value=('macos', 'x64')) as host, \
                    patch.object(intel.restore_ninja, 'binary_architectures', return_value={'x64'}) as arches, \
                    patch.object(intel.restore_ninja, 'probe_version', return_value='1.13.2') as version, \
                    patch.object(intel, 'frozen_ninja', return_value=124) as frozen:
                for key, value in dict(phase='after', continuation_stage=2, logical_stage=11, continuation_attempt='2',
                                       arch='arm64', run_id=intel.RUN, job_id=1, state={}).items():
                    path.write_text(json.dumps(dict(before, **{key: value})))
                    with self.assertRaises(ValueError):
                        latest.compile_tree(HISTORICAL, self.work, 1)
                path.write_text(json.dumps(before))
                for probe, value, original in ((host, ('macos', 'arm64'), ('macos', 'x64')),
                                                (arches, {'arm64'}, {'x64'}), (version, '1.12.1', '1.13.2')):
                    probe.return_value = value
                    with self.assertRaises(ValueError):
                        latest.compile_tree(HISTORICAL, self.work, 1)
                    probe.return_value = original
                for jobs in ('0', '-1', 'auto'):
                    with patch.dict(os.environ, CHROMIX_JOBS=jobs), self.assertRaises(ValueError):
                        latest.compile_tree(HISTORICAL, self.work, 1)
                frozen.assert_not_called()
                with patch.dict(os.environ, DYLD_LIBRARY_PATH='/foreign'):
                    self.assertEqual(latest.compile_tree(HISTORICAL, self.work, 1), 124)
                self.assertNotIn('DYLD_LIBRARY_PATH', frozen.call_args.args[-1])

    def test_ci_stage_real_ninja_pack_restore_and_fail_closed_graph(self):
        real_ninja(self)
        out = self.work / 'src/out/Default'
        args = (out / 'args.gn').read_bytes()
        sample_graph(out)
        (out / 'args.gn').write_bytes(args)
        # Only the graph/state binding is synthetic; donor receipts remain exact.
        state = intel.source_state(HISTORICAL, self.work)
        handoff = dict(self.handoff, state=state)
        old = dict(self.old, state=state)
        with patch.object(latest, 'donor_identity', return_value=(old, handoff)):
            self.admit()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            repo = base / 'repo'
            for name in ('build/posix/ci-stage.sh', 'build/posix/ci-parts.sh', 'build/posix/restore-snapshot.sh'):
                target = repo / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, target)
            tools = repo / 'tools'
            tools.mkdir()
            (tools / 'restore_posix_snapshot.py').write_text(
                f'import runpy, sys\nsys.path.insert(0, {str(ROOT / "tools")!r})\n'
                f'runpy.run_path({str(ROOT / "tools/restore_posix_snapshot.py")!r}, run_name="__main__")\n')
            (tools / 'restore_upstream_cache.py').write_text(
                'import sys\nassert sys.argv[1:3] == ["--phase", "verify"]\n')
            (tools / 'validate_macos156_x64_stage3_checkpoint.py').write_text(
                'import sys\nfrom pathlib import Path\nfrom unittest.mock import patch\n'
                f'sys.path.insert(0, {str(ROOT)!r})\n'
                'from tools import validate_macos156_x64_stage3_checkpoint as latest\n'
                'from tools.tests.test_macos156_x64_stage3_resume import emit_source\n'
                'intel = latest.intel\noriginal = latest.compile_tree\n'
                'def compile_tree(repo, work, stage):\n'
                '    assert original(repo, work, stage) == 0\n'
                '    return 124\n'
                f'with patch.object(latest, "donor_identity", return_value=({old!r}, {handoff!r})), '
                'patch.object(intel, "native_state"), '
                'patch.object(intel, "live_source", side_effect=emit_source), '
                'patch.object(intel.restore_ninja, "host_identity", return_value=("macos", "x64")), '
                'patch.object(intel.restore_ninja, "binary_architectures", return_value={"x64"}), '
                'patch.object(latest, "compile_tree", side_effect=compile_tree):\n'
                f'    raise SystemExit(latest.main([*sys.argv[1:], "--repo", {str(HISTORICAL)!r}]))\n')
            env = dict(os.environ, CHROMIX_UNCHANGED_RESUME='mac156-x64-stage3', COPYFILE_DISABLE='1',
                       TMPDIR='/tmp', GITHUB_OUTPUT=str(base / 'outputs'),
                       PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
            command = ['/bin/bash', str(repo / 'build/posix/ci-stage.sh'), '--platform', 'macos', '--arch', 'x64']
            first = subprocess.run([*command, '--workdir', str(self.work)], env=env, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertIn('upload_snapshot=true', (base / 'outputs').read_text())
            self.assertIn('snapshotting unfinished work', first.stderr)
            obj = out / 'sample.o'
            original = (obj.read_bytes(), obj.stat().st_mtime_ns)
            restore = base / 'restore'
            shutil.copytree(self.work / '.snapshot-stage-1', restore)
            next_work = base / 'next'
            env.update(GITHUB_RUN_ATTEMPT='2', CHROMIX_PREVIOUS_ATTEMPT='1')
            second = subprocess.run([*command, '--stage-index', '2', '--workdir', str(next_work),
                                     '--from-snapshot', str(restore)], env=env, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn('ninja: no work to do.', second.stdout)
            obj = next_work / 'src/out/Default/sample.o'
            self.assertEqual((obj.read_bytes(), obj.stat().st_mtime_ns), original)
            for stage, attempt in ((1, 1), (2, 2)):
                name = f'{latest.CONTINUATION}-s{stage}-after-attempt-{attempt}'
                for suffix in ('.json', '-source.json'):
                    self.assertTrue((next_work / 'fingerprint-diagnostics' / (name + suffix)).exists())
                receipt = json.loads((next_work / 'fingerprint-diagnostics' / (name + '.json')).read_bytes())
                self.assertEqual(receipt['logical_stage'], 11 + stage)
            self.assertFalse((next_work / 'src/out/Default/REGENERATED').exists())
            subgraph = next_work / 'src/out/Default/obj/test.ninja'
            subgraph.write_text('changed subgraph\n')
            env['CHROMIX_PREVIOUS_ATTEMPT'] = '2'
            third = subprocess.run([*command, '--stage-index', '3', '--workdir', str(next_work)],
                                   env=env, capture_output=True, text=True)
            self.assertNotEqual(third.returncode, 0)
            self.assertIn('GN graph changed', third.stderr)
            self.assertFalse((next_work / '.snapshot-stage-3').exists())
            self.assertEqual((obj.read_bytes(), obj.stat().st_mtime_ns), original)


class WorkflowTest(unittest.TestCase):
    def test_original_stage2_identity_and_native_recipes_are_unchanged(self):
        import ast
        name = 'tools/validate_macos156_x64_stage2_checkpoint.py'
        before = ast.parse(subprocess.check_output(['git', 'show', latest.SHA + ':' + name], cwd=ROOT))
        after = ast.parse((ROOT / name).read_bytes())
        for tree in (before, after):
            tree.body = [node for node in tree.body if isinstance(node, ast.Assign) or
                         isinstance(node, ast.FunctionDef) and node.name in (
                             'expected_manifest', 'producer_log', 'validate_metadata',
                             'download_identity', 'continuation_identity', 'donor_identity')]
        self.assertEqual(ast.dump(before), ast.dump(after))
        receipt = json.loads(FIXTURE['files'][previous.RECEIPT])
        self.assertEqual(intel.recipes(HISTORICAL), receipt['recipes'])
        self.assertEqual(intel.migration.source_ready_key(HISTORICAL, 'macos', 'x64'), intel.KEY)

    def test_shell_mode_is_exact_and_never_cold_falls_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            def run(mode, arch='x64'):
                return subprocess.run(['/bin/bash', str(ROOT / 'build/posix/ci-stage.sh'),
                                       '--platform', 'macos', '--arch', arch, '--workdir', str(work)],
                                      env=dict(os.environ, **intel_tests.ENV, CHROMIX_UNCHANGED_RESUME=mode),
                                      capture_output=True, text=True)
            for mode, arch in [('mac156-x64-stage3', 'x64'), ('mac156-x64-stage3', 'arm64'), ('unknown', 'x64')]:
                self.assertNotEqual(run(mode, arch).returncode, 0)
                self.assertFalse((work / 'src').exists())
            path = work / latest.RECEIPT
            path.parent.mkdir()
            path.write_text('{}')
            self.assertIn('requires the dedicated continuation mode', run('').stderr)
            self.assertFalse((work / 'src').exists())

    def test_fixed_main_stage3_entry_and_all_stage_dispatch(self):
        entry = yaml.safe_load((ROOT / '.github/workflows/build-mac156-x64-stage3-resume.yml').read_text())
        job = entry['jobs']['resume']
        self.assertEqual(job['if'], "github.ref == 'refs/heads/main'")
        values = job['with']
        for key, value in dict(platform='macos', arch='x64', runner='macos-26-intel',
                               resume_migration='mac156-x64-stage3', resume_run_id=str(latest.RUN),
                               resume_tree_stage='3', resume_attempt='1', resume_artifact_ids='13757361205',
                               build_profile='fast', use_upstream_cache=True).items():
            self.assertEqual(values[key], value)
        self.assertEqual(values['max-stages'], 8)
        workflow = (ROOT / '.github/workflows/build-posix-github.yml').read_bytes()
        subprocess.run([sys.executable, str(ROOT / 'tools/gen_posix_workflow.py')], check=True, capture_output=True)
        self.assertEqual(workflow, (ROOT / '.github/workflows/build-posix-github.yml').read_bytes())
        jobs = yaml.safe_load(workflow)['jobs']
        for stage in range(1, 9):
            steps = jobs[f'posix-{stage}']['steps']
            self.assertTrue(any(s.get('if') == "inputs.resume_migration == 'mac156-x64-stage3'"
                                and 'CHROMIX_UNCHANGED_RESUME=mac156-x64-stage3' in s.get('run', '') for s in steps))
            if stage > 1:
                producer = next(s for s in steps if s.get('name') == 'Record exact mac156 handoff producer attempt')
                self.assertIn("inputs.resume_migration == 'mac156-x64-stage3'", producer['if'])
                self.assertEqual(producer['env']['PRODUCER_ATTEMPT'],
                                 '${{ needs.posix-' + str(stage - 1) + '.outputs.snapshot_attempt }}')
            preserve = next(s for s in steps if s.get('id') == 'runtime_checkpoint')
            self.assertIn("inputs.resume_migration == 'mac156-x64-stage3'", preserve['if'])
        first = jobs['posix-1']['steps']
        gate = next(s for s in first if s.get('name') == 'Require exact mac156 Intel stage3 continuation inputs')
        self.assertIn('42735558594:3:1:13757361205', gate['run'])
        self.assertIn('refs/heads/main', gate['run'])
        valid = dict(values, **{'github.repository': intel.REPOSITORY, 'github.event_name': 'workflow_dispatch',
                               'github.ref': 'refs/heads/main'})
        def check(arguments):
            def replace(match):
                key = match[1]
                value = arguments.get(key, arguments.get(key.removeprefix('inputs.'), ''))
                return str(value).lower() if isinstance(value, bool) else str(value)
            script = re.sub(r'\$\{\{ (.*?) \}\}', replace, gate['run'])
            return subprocess.run(['/bin/bash', '-c', script], capture_output=True).returncode
        self.assertEqual(check(valid), 0)
        for key, value in dict(platform='linux', arch='arm64', runner='macos-26', artifact='foreign',
                               resume_run_id='1', resume_tree_stage='1', resume_attempt='2',
                               resume_artifact_ids=str(intel.ARTIFACT), build_profile='release', use_upstream_cache=False,
                               **{'max-stages': 7, 'github.repository': 'foreign/repo', 'github.event_name': 'push',
                                  'github.ref': 'refs/heads/other'}).items():
            self.assertNotEqual(check(dict(valid, **{key: value})), 0, key)
        self.assertIs(intel.frozen_ninja, arm.frozen_ninja)
        stage = (ROOT / 'build/posix/ci-stage.sh').read_text()
        self.assertIn('mac156-x64-stage3) UNCHANGED_HELPER=', stage)
        self.assertIn('fingerprint-diagnostics/mac156-x64-stage3-resume.json', stage)


if __name__ == '__main__':
    unittest.main()
