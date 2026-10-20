"""Exact Windows155.79 ARM64 stage12 continuation admission and controller tests."""
from copy import deepcopy
import gzip
import hashlib
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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import gen_windows_arm64_workflow as arm
import gen_windows15579_arm64_stage12_workflow as generator
import validate_windows15579_arm64_stage12_checkpoint as checkpoint

FIXTURE = ROOT / 'tools/tests/fixtures/windows15579_arm64_stage12_receipts.json.gz'


def fixture():
    return json.loads(gzip.decompress(FIXTURE.read_bytes()))


class Client:
    def __init__(self, data):
        self.data = data

    def get(self, path):
        return self.data['run'] if path == f'/actions/runs/{checkpoint.RUN}' else self.data['job']

    def items(self, path, key):
        return self.data['artifacts'][key]


class MetadataTest(unittest.TestCase):
    def validate(self, data):
        return checkpoint.validate_metadata(Client(data), log_loader=lambda: data['log'].encode())

    def test_real_producer_evidence(self):
        data = fixture()
        self.assertEqual(self.validate(data), checkpoint.manifest())
        self.assertIn('build did not finish within 12 stages', data['log'])
        self.assertIn(checkpoint.KEY, data['log'])
        self.assertNotRegex(data['log'], r'FAILED:|fatal error C\d|error C\d{4}')
        checkpoint.source_report(data['source'])
        self.assertEqual(hashlib.sha256((json.dumps(data['preparation'], sort_keys=True) + '\r\n').encode()).hexdigest(),
                         checkpoint.PREPARATION)
        self.assertEqual(data['preparation']['counters']['first_finish'], 0)
        self.assertEqual(data['preparation']['compiled_outputs']['removed_outputs'], 0)

    def test_exact_run_job_and_attempt_required(self):
        mutations = [('run', 'run_attempt', 2), ('run', 'head_sha', 'a' * 40),
                     ('run', 'conclusion', 'success'), ('run', 'path', 'other.yml'),
                     ('run', 'head_branch', 'main'), ('job', 'id', checkpoint.JOB + 1),
                     ('job', 'run_attempt', 2), ('job', 'run_id', checkpoint.RUN + 1),
                     ('job', 'labels', ['windows-11-arm']), ('job', 'conclusion', 'success')]
        for group, key, value in mutations:
            with self.subTest(group=group, key=key):
                data = fixture()
                data[group][key] = value
                with self.assertRaises(ValueError):
                    self.validate(data)

    def test_failed_or_duplicate_upload_rejected(self):
        for duplicate in (True, False):
            data = fixture()
            item = next(s for s in data['job']['steps'] if s['name'] == 'Upload tree part 2')
            if duplicate:
                data['job']['steps'].append(item)
            else:
                item['conclusion'] = 'failure'
            with self.assertRaises(ValueError):
                self.validate(data)

    def test_artifact_identity_and_complete_set(self):
        for key, value in [('id', 1), ('size_in_bytes', 1), ('digest', 'sha256:' + '0' * 64),
                           ('expired', True), ('name', 'win-arm64-tree-s12-attempt-2-part1')]:
            with self.subTest(key=key):
                data = fixture()
                item = next(a for a in data['artifacts']['artifacts'] if a['id'] == checkpoint.ARTIFACTS[0]['id'])
                item[key] = value
                with self.assertRaises(ValueError):
                    self.validate(data)
        for mode in ('missing', 'duplicate', 'wrong-origin'):
            data = fixture()
            items = data['artifacts']['artifacts']
            item = next(a for a in items if a['id'] == checkpoint.ARTIFACTS[0]['id'])
            if mode == 'missing':
                items.remove(item)
            elif mode == 'duplicate':
                items.append(item)
            else:
                item['workflow_run']['head_sha'] = '0' * 40
            with self.assertRaises(ValueError):
                self.validate(data)

    def test_log_is_exact_not_only_exhaustion_phrase(self):
        data = fixture()
        data['log'] += '\n'
        with self.assertRaisesRegex(ValueError, 'log digest'):
            self.validate(data)

    def test_source_identity_rejects_arch_version_patch_count_and_outputs(self):
        for mutation in ('version', 'patch_count', 'output', 'platform', 'status'):
            data = fixture()['source']
            if mutation == 'version':
                data['identity']['selection']['version'] = '155.0.8059.39'
            elif mutation == 'output':
                data['outputs']['base/BUILD.gn'] = '0' * 64
            elif mutation == 'platform':
                data['identity']['platform'] = 'macos'
            else:
                data[mutation] = 230 if mutation == 'patch_count' else 'failed'
            with self.assertRaises(ValueError):
                checkpoint.source_report(data)

    def test_original_windows_key_not_local_lf_key(self):
        identity, patches = checkpoint.patch_selection.select(ROOT, 'windows')
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode())
        for _, raw in patches:
            digest.update(raw)
        lite = ROOT / 'build/windows/lite-tarball-files/v8/test/torque/test-torque.tq'
        raw = lite.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
        digest.update(b'v8/test/torque/test-torque.tq')
        digest.update(raw)
        self.assertEqual(digest.hexdigest(), checkpoint.PATCH_KEY)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), fixture()['source']['identity']['lite']['files'][0]['sha256'])


class ArchiveTest(unittest.TestCase):
    def listing(self):
        names = ['.chromix-target-arch', 'src/.chromix-source-ready', 'src/.chromix-restored-patches.json',
                 'src/.chromix-upstream-restored.json', 'src/.chromix-restored-build-prepared.json',
                 'src/out/Default/args.gn', 'src/out/Default/build.ninja', 'src/out/Default/.ninja_log',
                 'src/out/Default/.ninja_deps', checkpoint.SOURCE_REPORT]
        return '\n\n'.join('Path = chromix/' + name + '\nSize = 10\nFolder = -' for name in names)

    def test_compiled_state_required(self):
        checkpoint.validate_listing(self.listing())
        with self.assertRaises(ValueError):
            checkpoint.validate_listing(self.listing().replace('src/out/Default/.ninja_deps', 'src/missing'))

    def test_path_link_duplicate_and_cold_rejection(self):
        for item in ['../escape', 'src/out/Chromix/a.obj', 'src/NUL', 'src/a:stream',
                     'src/.chromix-restored-patches-in-progress', 'src/.chromix-windows-migration.json',
                     'src/out/Default/args.gn']:
            with self.subTest(item=item), self.assertRaises(ValueError):
                checkpoint.validate_listing(self.listing() + '\n\nPath = chromix/' + item + '\nSize = 1')
        with self.assertRaises(ValueError):
            checkpoint.validate_listing(self.listing() + '\n\nPath = chromix/link\nSymbolic Link = /tmp\nSize = 1')


class AdmissionTest(unittest.TestCase):
    def test_target_sha_is_never_rewritten_to_donor(self):
        with patch.dict(os.environ, GITHUB_JOB='build-1', GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1'):
            with self.assertRaisesRegex(ValueError, 'target SHA'):
                checkpoint.verify(ROOT, ROOT, ROOT, 13, 'invalid', ROOT / 'unused.json')
        script = (ROOT / 'build/windows/ci-stage.ps1').read_text()
        self.assertNotRegex(script, r'\$env:GITHUB_SHA\s*=')

    def test_stage_and_consumer_gates(self):
        for stage, job in [(12, 'build-1'), (25, 'build-12'), (14, 'build-1')]:
            with patch.dict(os.environ, GITHUB_JOB=job), self.assertRaisesRegex(ValueError, 'stage/job'):
                checkpoint.verify(ROOT, ROOT, ROOT, stage, 'a' * 40, ROOT / 'unused.json')
        with patch.dict(os.environ, GITHUB_JOB='build-1', GITHUB_RUN_ID=str(checkpoint.RUN),
                        GITHUB_RUN_ATTEMPT='1', GITHUB_SHA='a' * 40):
            with self.assertRaisesRegex(ValueError, 'new consumer'):
                checkpoint.verify(ROOT, ROOT, ROOT, 13, 'a' * 40, ROOT / 'unused.json')

    def test_initial_and_later_hops_keep_actual_target_and_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            work, donor = root / 'work', root / 'donor'
            work.mkdir()
            donor.mkdir()
            temporary = root / 'temp'
            temporary.mkdir()
            (temporary / 'windows-snapshot.json').write_text(json.dumps(checkpoint.manifest()))
            (temporary / 'windows-snapshot-download.json').write_text(json.dumps(dict(
                status='success', phase='complete', publication='published', repository=checkpoint.REPOSITORY,
                head_sha=checkpoint.SHA, run_id=checkpoint.RUN, artifacts=checkpoint.ARTIFACTS)))
            state = {'.chromix-source-ready': ('abc',), 'out/Default/args.gn': ('def',),
                     'out/Default/build.ninja': ('graph',), 'out/Default/.ninja_log': ('log',)}
            env = dict(GITHUB_JOB='build-1', GITHUB_RUN_ID='123456', GITHUB_RUN_ATTEMPT='1',
                       GITHUB_SHA='a' * 40, RUNNER_TEMP=str(temporary))
            with patch.dict(os.environ, env), \
                    patch.object(checkpoint, 'repository_identity', return_value=('a' * 40, [])), \
                    patch.object(checkpoint, 'source_state', return_value=state), \
                    patch.object(checkpoint, 'host_tools'), \
                    patch.object(checkpoint.stack, 'verify', return_value=fixture()['source']), \
                    patch.object(checkpoint.safe, '_unchanged'):
                result = checkpoint.verify(work, donor, ROOT, 13, 'a' * 40,
                                           work / 'fingerprint-diagnostics/first-source.json')
                self.assertEqual(result['target_sha'], 'a' * 40)
                self.assertEqual(result['preparation_key'], checkpoint.KEY)
                self.assertEqual(result['changed_files'], [])
                with self.assertRaisesRegex(ValueError, 'already admitted'):
                    checkpoint.verify(work, donor, ROOT, 13, 'a' * 40,
                                      work / 'fingerprint-diagnostics/repeated.json')
                os.environ['GITHUB_JOB'] = 'build-2'
                state['out/Default/.ninja_log'] = ('continued-log',)
                checkpoint.verify(work, donor, ROOT, 14, 'a' * 40,
                                  work / 'fingerprint-diagnostics/second-source.json')
                state['out/Default/args.gn'] = ('changed-args',)
                with self.assertRaisesRegex(ValueError, 'admission mismatch'):
                    checkpoint.verify(work, donor, ROOT, 14, 'a' * 40,
                                      work / 'fingerprint-diagnostics/changed-source.json')

    def test_recipe_allowlist_excludes_all_source_inputs(self):
        self.assertEqual([p for p in checkpoint.ALLOWED if p.startswith('build/')], ['build/windows/ci-stage.ps1'])
        self.assertFalse(any(p.startswith('patches/') for p in checkpoint.ALLOWED))
        for name in ['tools/prepare_restored_build.py', 'tools/patch_selection.py', 'build/upstream-cache.json',
                     'build/windows/prepare-ungoogled.ps1', 'build/args.windows.arm64.gn']:
            self.assertNotIn(name, checkpoint.ALLOWED)

    def test_resume_branch_never_runs_prepare_or_gn_mutation(self):
        script = (ROOT / 'build/windows/ci-stage.ps1').read_text()
        start = script.index('if ($ResumeStage12DonorRepo) {\n  if (-not $RestoredUpstream)')
        body = script[start:script.index('\n} else {', start)]
        for forbidden in ['prepare-ungoogled.ps1', 'prepare_restored_build.py', 'merge_gn_args.py',
                          '--configure', '$gn gen', 'apply_restored_patches.py', 'Remove-Item']:
            self.assertNotIn(forbidden, body)
        self.assertLess(body.index('unchanged-source admission failed'), body.index('Write-OutVar snapshot_safe true'))
        self.assertIn('--source-output $FingerprintSourceReport', body)
        self.assertIn('--download', body)
        self.assertIn('-n chrome', body)
        self.assertIn('$MaxStages -ne 24', script)
        self.assertIn('$Arch -cne \'arm64\'', script)
        self.assertIn('Write-OutVar snapshot_safe $(if ($ResumeStage12DonorRepo', script)


class WorkflowTest(unittest.TestCase):
    def test_generated_workflow_current(self):
        self.assertEqual(generator.OUTPUT.read_text(), generator.render())
        parsed = yaml.safe_load(generator.render())
        self.assertEqual(parsed, generator.workflow())

    def test_fixed_restore_no_latest_or_cold_inputs(self):
        flow = generator.workflow()
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(set(flow['on']['workflow_dispatch']['inputs']), {'compile_jobs'})
        self.assertEqual(flow['env']['CHROMIX_BUILD_PROFILE'], 'native')
        self.assertEqual(flow['env']['CHROMIX_USE_UPSTREAM_CACHE'], '1')
        self.assertNotIn('actions/cache', generator.render())
        for forbidden in ['latest-cache', 'fetch_upstream_cache.py', 'resume_run_id']:
            self.assertNotIn(forbidden, generator.render())
        first = flow['jobs']['build-1']['steps']
        self.assertTrue(any('download_windows_snapshot.py' in s.get('run', '') for s in first))
        self.assertTrue(any('--metadata' in s.get('run', '') for s in first))
        self.assertFalse(any(s.get('uses') == 'actions/download-artifact@v4' for s in first))

    def test_twelve_additional_stages_preserve_exact_attempt_and_failures(self):
        flow = generator.workflow()
        self.assertEqual(set(flow['jobs']), {f'build-{i}' for i in range(1, 13)} | {'complete', 'verify-arm64'})
        for index in range(1, 13):
            job = flow['jobs'][f'build-{index}']
            steps = job['steps']
            stage = next(s for s in steps if s.get('id') == 'stage')
            self.assertIn(f'-StageIndex {index + 12} -MaxStages 24 -FromArtifact', stage['run'])
            self.assertIn('-ResumeStage12DonorRepo', stage['run'])
            self.assertNotIn('continue-on-error', stage)
            self.assertEqual(job['runs-on'], 'windows-2022')
            donor = next(s for s in steps if s.get('name') == 'Checkout exact donor as data')
            self.assertEqual(donor['with']['ref'], checkpoint.SHA)
            if index > 1:
                download = next(s for s in steps if s.get('uses') == 'actions/download-artifact@v4')
                self.assertEqual(download['with']['pattern'],
                                 f'win-arm64-tree-s{index+11}-attempt-${{{{ needs.build-{index-1}.outputs.snapshot_attempt }}}}-part*')
                self.assertNotIn('run-id', download['with'])
                self.assertIn(f'needs.build-{index-1}.result == \'success\'', job['if'])
            for part in range(1, 5):
                upload = next(s for s in steps if s.get('name') == f'Upload tree part {part}')
                self.assertEqual(upload['with']['name'],
                                 f'win-arm64-tree-s{index+12}-attempt-${{{{ github.run_attempt }}}}-part{part}')
                self.assertIn('always()', upload['if'])
                self.assertIn("snapshot_safe == 'true'", upload['if'])
                self.assertNotIn('finished', upload['if'])
                self.assertEqual(upload['with']['retention-days'], 7)

    def test_native_acceptance_is_unchanged(self):
        flow = generator.workflow()
        self.assertEqual(flow['jobs']['verify-arm64'], arm.workflow()['jobs']['verify-arm64'])
        verify = flow['jobs']['verify-arm64']
        self.assertEqual(verify['runs-on'], 'windows-11-arm')
        text = json.dumps(verify)
        for term in ['--native', '--arch arm64', '--source-report', 'fingerprint_acceptance.py', 'always()']:
            self.assertIn(term, text)

    def test_all_powershell_parses(self):
        pwsh = os.environ.get('CHROMIX_TEST_PWSH')
        if not pwsh:
            self.skipTest('CHROMIX_TEST_PWSH not set')
        scripts = [(ROOT / 'build/windows/ci-stage.ps1').read_text()]
        for job in generator.workflow()['jobs'].values():
            scripts.extend(s['run'] for s in job['steps'] if s.get('shell') in ('pwsh', 'powershell'))
        with tempfile.TemporaryDirectory() as directory:
            for index, script in enumerate(scripts):
                Path(directory, f'{index}.ps1').write_text(script)
            command = '$bad=0; Get-ChildItem -LiteralPath $args[0] -Filter *.ps1 | ForEach-Object { $t=$null; $e=$null; [Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$t,[ref]$e) | Out-Null; if ($e.Count) { $e | Out-String | Write-Host; $bad++ } }; exit $bad'
            runner = Path(directory, 'parse.ps1')
            runner.write_text(command)
            subprocess.run([pwsh, '-NoProfile', '-File', str(runner), directory], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
