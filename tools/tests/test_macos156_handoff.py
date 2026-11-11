"""Real receipt sidecars, snapshot lifecycle, and fixed latest-stage1 recovery."""
import copy
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from tools import validate_macos156_arm64_stage8_checkpoint as arm
from tools import validate_macos156_x64_stage8_checkpoint as intel
from tools import validate_macos156_arm64_stage1_checkpoint as latest
from tools.tests import test_macos156_arm64_stage8_resume as arm_tests
from tools.tests.test_macos156_arm64_stage8_resume import sample_graph, real_ninja
from tools.tests import test_macos156_x64_stage8_resume as intel_tests

from tools.tests.macos156_historical import recipe_repo

ROOT = Path(__file__).resolve().parents[2]
HISTORICAL = recipe_repo()
FIXTURE = json.loads(gzip.decompress(
    (ROOT / 'tools/tests/fixtures/macos156_arm64_stage1_receipts.json.gz').read_bytes()))


LIVE_SOURCE = arm.live_source


def emit_source(repo, work, output):
    # Keep live_source's validation and write its actual sidecar, not a no-op mock.
    original = arm.subprocess.run

    def run(command, *args, **kwargs):
        if 'verify_patch_stack.py' in command[1]:
            output.write_bytes((work / 'fingerprint-diagnostics/source-final.json').read_bytes())
            return subprocess.CompletedProcess(command, 0)
        return original(command, *args, **kwargs)

    with patch.object(arm.subprocess, 'run', side_effect=run):
        LIVE_SOURCE(repo, work, output)


class HandoffMixin:
    def test_real_sidecar_lifecycle_and_complete_identity_rejection(self):
        helper = self.helper
        self.admit()
        with patch.object(helper, 'native_state'), patch.object(helper, 'live_source', side_effect=emit_source):
            helper.continuation(HISTORICAL, self.work, 1, 'before')
            (self.work / 'src/out/Default/.ninja_log').write_text('# ninja log v7\ncompiled stage1\n')
            after = helper.continuation(HISTORICAL, self.work, 1, 'after')
            prefix = f'mac156-{after["arch"]}-stage8-continuation'
            directory = self.work / 'fingerprint-diagnostics'
            path = directory / f'{prefix}-s1-after-attempt-1.json'
            self.assertTrue(path.with_name(path.stem + '-source.json').is_file())
            self.assertEqual(len(list(directory.glob(f'{prefix}-s1-after-attempt-*.json'))), 2)
            for field, value in dict(phase='before', continuation_stage=2, logical_stage=7,
                                     continuation_run='8888', continuation_attempt='2', arch='wrong',
                                     run_id=1, attempt=99, job_id=1, head_sha='a' * 40, stage=7,
                                     version='155', preparation_key='other', source_identity='other',
                                     artifact_id=1, state={}).items():
                with self.subTest(field=field):
                    path.write_text(json.dumps(dict(after, **{field: value})))
                    with self.assertRaises(ValueError):
                        helper.continuation(HISTORICAL, self.work, 2, 'before')
            path.write_text(json.dumps(after))
            for attempt in ('', '2', '0'):
                with patch.dict(os.environ, CHROMIX_PREVIOUS_ATTEMPT=attempt), self.assertRaises(ValueError):
                    helper.continuation(HISTORICAL, self.work, 2, 'before')
            duplicate = directory / f'{prefix}-s1-after-attempt-2.json'
            duplicate.write_text(json.dumps(dict(after, continuation_attempt='2')))
            with self.assertRaisesRegex(ValueError, 'exact previous'):
                helper.continuation(HISTORICAL, self.work, 2, 'before')
            duplicate.unlink()
            # Failed-jobs reruns consume the successful producer's older attempt.
            with patch.dict(os.environ, GITHUB_RUN_ATTEMPT='2', CHROMIX_PREVIOUS_ATTEMPT='1'):
                result = helper.continuation(HISTORICAL, self.work, 2, 'before')
                self.assertEqual(result['continuation_attempt'], '2')
                helper.continuation(HISTORICAL, self.work, 2, 'after')
            with patch.dict(os.environ, GITHUB_RUN_ATTEMPT='1', CHROMIX_PREVIOUS_ATTEMPT='2'):
                with self.assertRaises(ValueError):
                    helper.continuation(HISTORICAL, self.work, 3, 'before')
            with patch.dict(os.environ, GITHUB_RUN_ATTEMPT='2', CHROMIX_PREVIOUS_ATTEMPT='2'):
                helper.continuation(HISTORICAL, self.work, 3, 'before')


class ArmHandoffTest(HandoffMixin, unittest.TestCase):
    helper = arm
    setUpClass = classmethod(arm_tests.ReceiptTest.setUpClass.__func__)
    tearDownClass = classmethod(arm_tests.ReceiptTest.tearDownClass.__func__)
    setUp = arm_tests.ReceiptTest.setUp
    admit = arm_tests.ReceiptTest.admit

    def test_ci_stage_compile_stamp_pack_restore_then_next_attempt(self):
        real_ninja(self)
        if arm.restore_ninja.host_identity() != ('macos', 'arm64'):
            self.skipTest('native macOS ARM64 required')
        out = self.work / 'src/out/Default'
        args = (out / 'args.gn').read_bytes()
        sample_graph(out)
        (out / 'args.gn').write_bytes(args)
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
            (tools / 'validate_macos156_arm64_stage8_checkpoint.py').write_text(
                'import sys\nfrom pathlib import Path\nfrom unittest.mock import patch\n'
                f'sys.path.insert(0, {str(ROOT)!r})\n'
                'from tools import validate_macos156_arm64_stage8_checkpoint as arm\n'
                'from tools.tests.test_macos156_handoff import emit_source\n'
                'original = arm.compile_tree\n'
                'def compile_tree(repo, work, stage):\n'
                '    assert original(repo, work, stage) == 0\n'
                '    return 124\n'
                'with patch.object(arm, "native_state"), '
                'patch.object(arm, "live_source", side_effect=emit_source), '
                'patch.object(arm, "compile_tree", side_effect=compile_tree):\n'
                f'    raise SystemExit(arm.main([*sys.argv[1:], "--repo", {str(HISTORICAL)!r}]))\n')
            env = dict(os.environ, CHROMIX_UNCHANGED_RESUME='mac156-arm64-stage8',
                       COPYFILE_DISABLE='1', TMPDIR='/private/tmp', GITHUB_OUTPUT=str(base / 'outputs'),
                       PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
            first = subprocess.run(['/bin/bash', str(repo / 'build/posix/ci-stage.sh'),
                                    '--platform', 'macos', '--arch', 'arm64', '--workdir', str(self.work)],
                                   env=env, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertIn('upload_snapshot=true', (base / 'outputs').read_text())
            first_object = (out / 'sample.o').stat()
            restore = base / 'restore'
            shutil.copytree(self.work / '.snapshot-stage-1', restore)
            second_work = base / 'next'
            env.update(GITHUB_RUN_ATTEMPT='2', CHROMIX_PREVIOUS_ATTEMPT='1')
            second = subprocess.run(['/bin/bash', str(repo / 'build/posix/ci-stage.sh'),
                                     '--platform', 'macos', '--arch', 'arm64', '--stage-index', '2',
                                     '--workdir', str(second_work), '--from-snapshot', str(restore)],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn('ninja: no work to do.', second.stdout)
            obj = second_work / 'src/out/Default/sample.o'
            self.assertEqual(obj.stat().st_mtime_ns, first_object.st_mtime_ns)
            self.assertEqual(obj.read_bytes(), (out / 'sample.o').read_bytes())
            self.assertTrue((second_work / '.snapshot-stage-2/p1/tree.tar.zst.001').is_file())
            for stage, attempt in ((1, 1), (2, 2)):
                for suffix in ('', '-source'):
                    name = f'mac156-arm64-stage8-continuation-s{stage}-after-attempt-{attempt}{suffix}.json'
                    self.assertTrue((second_work / 'fingerprint-diagnostics' / name).is_file())
            self.assertFalse((second_work / 'src/out/Default/REGENERATED').exists())


class IntelHandoffTest(HandoffMixin, unittest.TestCase):
    helper = intel
    setUpClass = classmethod(intel_tests.ReceiptTest.setUpClass.__func__)
    tearDownClass = classmethod(intel_tests.ReceiptTest.tearDownClass.__func__)
    setUp = intel_tests.ReceiptTest.setUp
    admit = intel_tests.ReceiptTest.admit


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
    def test_exact_stage1_admission_then_two_new_stages_preserve_donor_receipts(self):
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
                    patch.object(arm, 'native_state'), patch.object(arm, 'live_source', side_effect=emit_source), \
                    patch.object(arm.migration.restore, 'verify_restored'), patch.object(arm.migration, '_verify_tooling'):
                bad_state = dict(state, **{'out/Default/.ninja_log': 'wrong'})
                with patch.object(arm, 'source_state', return_value=bad_state), self.assertRaises(ValueError):
                    latest.admit(work, donor, HISTORICAL, origin, report)
                result = latest.admit(work, donor, HISTORICAL, origin, report)
                self.assertEqual(result['state'], state)
                self.assertEqual(result['provenance']['manifest'], latest.expected_manifest())
                self.assertEqual(result['changed_files'], [])
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
                    self.assertEqual(before['logical_stage'], 11)
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


class LatestMetadataTest(unittest.TestCase):
    def metadata(self, client=None):
        return latest.validate_metadata(client or Client(), arm.REPOSITORY, [a['id'] for a in latest.ARTIFACTS],
                                        log_loader=lambda: FIXTURE['producer_log'].encode())

    def test_actual_successful_stage1_and_two_independently_pinned_artifacts(self):
        self.assertEqual(self.metadata(), latest.expected_manifest())
        self.assertEqual(sum(latest.VOLUME_SIZES), 9753877946)
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            for name, text in FIXTURE['files'].items():
                path = work / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            latest.donor_identity(work)
            receipt = json.loads(FIXTURE['files'][arm.RECEIPT])
            self.assertEqual(arm.recipes(HISTORICAL), receipt['recipes'])
            handoff = work / 'fingerprint-diagnostics/mac156-arm64-stage8-continuation-s1-after-attempt-1.json'
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
                        conclusion='failure', name='stage 2'),
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

    def test_main_entry_and_all_handoff_attempts_are_fixed(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/build-mac156-arm64-stage1-resume.yml').read_text())
        job = workflow['jobs']['resume']
        self.assertEqual(job['if'], "github.ref == 'refs/heads/main'")
        values = job['with']
        self.assertEqual(values['resume_run_id'], str(latest.RUN))
        self.assertEqual(values['resume_artifact_ids'], '13706643929,13707496932')
        self.assertEqual(values['resume_migration'], 'mac156-arm64-stage1')
        self.assertEqual(values['resume_tree_stage'], '1')
        self.assertEqual(values['max-stages'], 8)
        jobs = yaml.safe_load((ROOT / '.github/workflows/build-posix-github.yml').read_text())['jobs']
        for stage in range(1, 9):
            steps = jobs[f'posix-{stage}']['steps']
            self.assertTrue(any(s.get('if') == "inputs.resume_migration == 'mac156-arm64-stage1'"
                                and 'CHROMIX_UNCHANGED_RESUME=mac156-arm64-stage1' in s.get('run', '') for s in steps))
            if stage > 1:
                producer = next(s for s in steps if s.get('name') == 'Record exact mac156 handoff producer attempt')
                self.assertIn('CHROMIX_PREVIOUS_ATTEMPT=$PRODUCER_ATTEMPT', producer['run'])
                self.assertNotIn('linux', producer['if'])
                self.assertEqual(producer['env']['PRODUCER_ATTEMPT'],
                                 '${{ needs.posix-' + str(stage - 1) + '.outputs.snapshot_attempt }}')


if __name__ == '__main__':
    unittest.main()
