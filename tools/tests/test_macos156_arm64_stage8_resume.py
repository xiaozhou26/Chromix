"""Fixed mac156 latest-stage8 provenance and unchanged-source continuation contracts."""
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

from tools import validate_macos156_arm64_stage8_checkpoint as arm

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads(gzip.decompress(
    (ROOT / 'tools/tests/fixtures/macos156_arm64_stage8_receipts.json.gz').read_bytes()))
GN_FIXTURE = json.loads(gzip.decompress(
    (ROOT / 'tools/tests/fixtures/macos156_donor_gn_graph.json.gz').read_bytes()))
ENV = {'GITHUB_RUN_ID': '999999', 'GITHUB_RUN_ATTEMPT': '1', 'CHROMIX_PREVIOUS_ATTEMPT': '1',
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

    def test_compile_real_ninja_with_fresh_and_unchanged_receipts(self):
        real_ninja(self)
        if arm.restore_ninja.host_identity() != ('macos', 'arm64'):
            self.skipTest('receipt compile requires native macOS ARM64')
        out = self.work / 'src/out/Default'
        original_args = (out / "args.gn").read_bytes()
        sample_graph(out)
        (out / "args.gn").write_bytes(original_args)
        self.admit()
        with patch.object(arm, 'native_state'), patch.object(arm, 'live_source'):
            arm.continuation(ROOT, self.work, 1, 'before')
        proof = arm.source_state(ROOT, self.work)
        self.assertEqual(set(proof), {'out/Default/build.ninja', 'out/Default/.ninja_log',
                                     'out/Default/.ninja_deps', 'graph'})
        self.assertEqual(arm.compile_tree(ROOT, self.work, 1), 0)
        self.assertTrue((out / 'sample.o').is_file())
        self.assertFalse((out / 'REGENERATED').exists())
        self.assertEqual(arm.source_state(ROOT, self.work)['graph'], proof['graph'])
        unchanged = arm.output_inventory(out)
        with patch.object(arm, 'native_state'), patch.object(arm, 'live_source'):
            arm.continuation(ROOT, self.work, 1, 'after')
            arm.continuation(ROOT, self.work, 2, 'before')
        self.assertEqual(arm.compile_tree(ROOT, self.work, 2), 0)
        self.assertEqual(arm.output_inventory(out)['sample.o'], unchanged['sample.o'])
        with patch.dict(os.environ, GITHUB_RUN_ATTEMPT='2'):
            with self.assertRaises((ValueError, RuntimeError)):
                arm.compile_tree(ROOT, self.work, 2)
        (out / 'build.ninja').write_text((out / 'build.ninja').read_text() + '# changed\n')
        with self.assertRaises(ValueError):
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


def real_ninja(test):
    path = shutil.which('ninja')
    if not path or not shutil.which('cc'):
        test.skipTest('real Ninja and C compiler required')
    if subprocess.check_output([path, '--version'], text=True).strip() != '1.13.2':
        test.skipTest('exact donor Ninja 1.13.2 required')
    return Path(path).resolve()


def sample_graph(out, dependency='', extra=''):
    (out / 'sample.c').write_text('int answer(void) { return 42; }\n')
    (out / 'args.gn').write_text('# fixture GN args\n')
    (out / 'build.ninja').write_text(
        'rule regen\n  command = touch REGENERATED; exit 23\n  generator = 1\n'
        'build build.ninja: regen always\nbuild always: phony\n'
        'rule cc\n  command = cc -c sample.c -o sample.o\n'
        'build sample.o: cc sample.c' + dependency + '\n'
        'build chrome: phony sample.o\n' + extra)


class RealNinjaTest(unittest.TestCase):
    def setUp(self):
        self.ninja = real_ninja(self)
        self.tmp = tempfile.TemporaryDirectory(prefix='mac156-real-ninja-')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name).resolve()
        self.out = self.work / 'src/out/Default'
        self.out.mkdir(parents=True)
        (self.work / 'fingerprint-diagnostics').mkdir()
        sample_graph(self.out)

    def run_frozen(self):
        return arm.frozen_ninja(self.ninja, self.work, '2', dict(os.environ))

    def test_actual_supported_modes_and_fresh_build_without_regeneration(self):
        modes = subprocess.run([str(self.ninja), '-d', 'list'], capture_output=True, text=True)
        self.assertIn('keepdepfile', modes.stdout)
        self.assertNotIn('norebuild', modes.stdout)
        bad = subprocess.run([str(self.ninja), '-d', 'norebuild'], capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        normal = subprocess.run([str(self.ninja), '-C', str(self.out), '-n', '-v', 'chrome'],
                                capture_output=True, text=True, check=True)
        self.assertIn('touch REGENERATED', normal.stdout)
        original = arm.output_inventory(self.out)
        graph = (self.out / 'build.ninja').read_bytes()
        self.assertEqual(self.run_frozen(), 0)
        self.assertTrue((self.out / 'sample.o').is_file())
        self.assertFalse((self.out / 'REGENERATED').exists())
        self.assertEqual(graph, (self.out / 'build.ninja').read_bytes())
        for name in ('sample.c', 'args.gn', 'build.ninja'):
            self.assertEqual(original[name], arm.output_inventory(self.out)[name])
        obj = arm.output_inventory(self.out)['sample.o']
        self.assertEqual(self.run_frozen(), 0)
        self.assertEqual(obj, arm.output_inventory(self.out)['sample.o'])
        self.assertFalse(list((self.work / 'fingerprint-diagnostics').rglob('entry.ninja')))
        self.assertTrue(list((self.work / 'fingerprint-diagnostics').rglob('regeneration-proof.json')))

    def test_explicit_implicit_order_only_and_validation_regeneration_rejected(self):
        for dependency in (' build.ninja', ' | build.ninja', ' || build.ninja', ' |@ build.ninja'):
            with self.subTest(dependency=dependency):
                sample_graph(self.out, dependency)
                original = arm.output_inventory(self.out)
                with self.assertRaisesRegex(ValueError, 'regeneration.*forbidden'):
                    self.run_frozen()
                self.assertEqual(original, arm.output_inventory(self.out))
                self.assertFalse((self.out / 'sample.o').exists())
                self.assertFalse((self.out / 'REGENERATED').exists())

    def test_nested_manifest_and_args_generation_rejected(self):
        for name in ('nested.ninja', 'args.gn'):
            with self.subTest(name=name):
                (self.out / 'nested.ninja').write_text('# unchanged subgraph\n')
                sample_graph(self.out, ' |@ ' + name,
                             f'include nested.ninja\nbuild {name}: regen always\n')
                original = arm.output_inventory(self.out)
                with self.assertRaisesRegex(ValueError, 'regeneration.*forbidden'):
                    self.run_frozen()
                self.assertEqual(original, arm.output_inventory(self.out))

    def test_relative_paths_spaces_subninja_and_existing_object_reuse(self):
        nested = self.out / 'sub'
        nested.mkdir()
        (self.out / 'sample.c').rename(self.out / 'source file.c')
        graph = (self.out / 'build.ninja').read_text()
        graph = graph[:graph.index('rule cc')] + 'subninja sub/objects.ninja\n'
        (self.out / 'build.ninja').write_text(graph)
        (nested / 'objects.ninja').write_text(
            'rule cc\n  command = cc -c "source file.c" -o sample.o\n'
            'build sample.o: cc source$ file.c\nbuild chrome: phony sample.o\n')
        original = arm.output_inventory(self.out)
        self.assertEqual(self.run_frozen(), 0)
        self.assertFalse((self.out / 'REGENERATED').exists())
        current = arm.output_inventory(self.out)
        for name in ('build.ninja', 'sub/objects.ninja', 'source file.c', 'args.gn'):
            self.assertEqual(current[name], original[name])
        self.assertEqual(self.run_frozen(), 0)
        self.assertEqual(arm.output_inventory(self.out)['sample.o'], current['sample.o'])

    def test_depfile_discovered_manifest_regeneration_rejected(self):
        graph = (self.out / 'build.ninja').read_text().replace(
            '  command = cc -c sample.c -o sample.o',
            '  command = cc -c sample.c -o sample.o\n  depfile = sample.o.d')
        (self.out / 'build.ninja').write_text(graph)
        (self.out / 'sample.o.d').write_text('sample.o: build.ninja\n')
        original = arm.output_inventory(self.out)
        with self.assertRaisesRegex(ValueError, 'regeneration.*forbidden'):
            self.run_frozen()
        self.assertEqual(original, arm.output_inventory(self.out))

    def load_real_gn(self):
        for name, text in GN_FIXTURE['generated_files'].items():
            path = self.work / 'src' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        # Match the real donor self-invocation path without running GN on the restored graph.
        path = self.out / 'build.ninja'
        path.write_text(path.read_text().replace('../../../gn ', './gn '))
        (self.out / 'gn').write_text('#!/bin/sh\ntouch REGENERATED\nexit 23\n')
        (self.out / 'gn').chmod(0o755)

    def test_exact_donor_generated_phony_stamp_header_and_real_c_build(self):
        self.load_real_gn()
        self.assertTrue((self.out / 'build.ninja').read_text().startswith(GN_FIXTURE['donor_header']))
        self.assertEqual(GN_FIXTURE['donor_gn_version'], '2578 (510ec7992c30)')
        self.assertEqual(arm.sha(GN_FIXTURE['gn_writer_source'].encode()), GN_FIXTURE['gn_writer_sha256'])
        self.assertEqual(GN_FIXTURE['actual_probes']['direct.txt'], '')
        command = './gn --root=../.. -q --fail-on-unused-args --regeneration gen .'
        self.assertEqual(GN_FIXTURE['actual_probes']['transitive.txt'].strip(), command)
        original = arm.output_inventory(self.out)
        self.assertEqual(self.run_frozen(), 0)
        self.assertTrue(list(self.out.rglob('*.o')))
        self.assertFalse((self.out / 'REGENERATED').exists())
        proof = json.loads(next((self.work / 'fingerprint-diagnostics').rglob('regeneration-proof.json')).read_text())
        self.assertEqual(proof['commands'], [command])
        self.assertEqual(proof['nodes'], GN_FIXTURE['actual_proof']['nodes'])
        self.assertEqual(proof['producers'], GN_FIXTURE['actual_proof']['producers'])
        self.assertIn('args.gn', proof['unproduced'])
        for name in ('build.ninja', 'toolchain.ninja', 'build.ninja.stamp'):
            self.assertEqual(original[name], arm.output_inventory(self.out)[name])
        obj = {k: v for k, v in arm.output_inventory(self.out).items() if k.endswith('.o')}
        self.assertEqual(self.run_frozen(), 0)
        self.assertEqual(obj, {k: v for k, v in arm.output_inventory(self.out).items() if k.endswith('.o')})

    def test_real_gn_indirect_stamp_generation_cannot_enter_compile_plan(self):
        for dependency in ('build.ninja', 'build.ninja.stamp'):
            with self.subTest(dependency=dependency):
                self.load_real_gn()
                path = self.out / 'build.ninja'
                path.write_text(path.read_text().replace('build chrome: phony phony/chrome',
                                                        'build chrome: phony phony/chrome |@ ' + dependency))
                (self.out / 'build.ninja.stamp').unlink()
                original = arm.output_inventory(self.out)
                with self.assertRaisesRegex(ValueError, 'regeneration.*forbidden'):
                    self.run_frozen()
                self.assertEqual(original, arm.output_inventory(self.out))
                self.assertFalse((self.out / 'REGENERATED').exists())

    def test_real_gn_no_producer_requires_complete_graph_proof(self):
        self.load_real_gn()
        path = self.out / 'build.ninja'
        text = path.read_text()
        # Model an externally generated frozen graph using the actual GN output as the base.
        text = text.replace(GN_FIXTURE['donor_header'], 'ninja_required_version = 1.7.2\n\n')
        path.write_text(text)
        self.assertEqual(self.run_frozen(), 0)
        proof = json.loads(next((self.work / 'fingerprint-diagnostics').rglob('regeneration-proof.json')).read_text())
        self.assertEqual(proof['commands'], [])
        self.assertEqual(proof['nodes'], {})
        self.assertIn('build.ninja', proof['unproduced'])
        self.assertIn('build.ninja.stamp', proof['unproduced'])
        self.assertIn('args.gn', proof['unproduced'])

    def test_phony_only_empty_closure_is_proved_not_assumed(self):
        self.load_real_gn()
        path = self.out / 'build.ninja'
        path.write_text(path.read_text().replace('build build.ninja.stamp: gn',
                                                'build build.ninja.stamp: phony'))
        self.assertEqual(self.run_frozen(), 0)
        proof = json.loads(next((self.work / 'fingerprint-diagnostics').rglob('regeneration-proof.json')).read_text())
        self.assertEqual(proof['commands'], [])
        self.assertEqual(proof['nodes']['build.ninja.stamp']['rule'], 'phony')

    def test_phony_validation_producer_is_traced_and_blocked(self):
        self.load_real_gn()
        path = self.out / 'build.ninja'
        text = path.read_text().replace('build build.ninja: phony build.ninja.stamp',
                                       'build build.ninja: phony |@ bridge')
        text += '\nbuild bridge: phony build.ninja.stamp\n'
        text = text.replace('build chrome: phony phony/chrome',
                            'build chrome: phony phony/chrome |@ build.ninja')
        path.write_text(text)
        (self.out / 'build.ninja.stamp').unlink()
        with self.assertRaisesRegex(ValueError, 'regeneration.*forbidden'):
            self.run_frozen()
        self.assertFalse((self.out / 'REGENERATED').exists())

    def test_unknown_or_incomplete_query_cannot_justify_empty_commands(self):
        for query in ('build.ninja:\n  unknown: future-format\n',
                      'other.ninja:\n  outputs:\n', ''):
            def inspect(args):
                if args == ['-t', 'targets', 'all']:
                    return 'build.ninja: phony\n'
                return query
            with self.subTest(query=query), self.assertRaises(ValueError):
                arm.regeneration_proof(self.out, [self.out / 'build.ninja'], self.work / 'entry.ninja', inspect)

    def test_empty_nonphony_command_and_warnings_are_rejected_with_diagnostics(self):
        self.load_real_gn()
        path = self.out / 'build.ninja'
        path.write_text(path.read_text().replace('command = ./gn --root=../.. -q --fail-on-unused-args --regeneration gen .',
                                                 'command = $unknown_command'))
        original = arm.output_inventory(self.out)
        with self.assertRaisesRegex(ValueError, 'no identifiable command'):
            self.run_frozen()
        self.assertEqual(original, arm.output_inventory(self.out))
        self.assertTrue(list((self.work / 'fingerprint-diagnostics').rglob('*.stdout')))
        self.load_real_gn()
        (self.out / '.ninja_log').write_text('bad header\n')
        with self.assertRaisesRegex(ValueError, 'warnings'):
            self.run_frozen()
        self.assertTrue(any(p.read_text() for p in (self.work / 'fingerprint-diagnostics').rglob('*.stderr')))

    def test_real_donor_response_rule_reproduces_and_isolates_dryrun_mutation(self):
        fixture = json.loads(gzip.decompress(
            (ROOT / 'tools/tests/fixtures/macos156_preflight_state.json.gz').read_bytes()))
        self.assertEqual(fixture['cloud_before']['state'], fixture['cloud_after']['state'])
        self.assertEqual(fixture['isolated_full_source_clone']['entries'], 952203)
        self.assertEqual(fixture['isolated_full_source_difference']['original_changed'], {})
        self.assertEqual(len(fixture['isolated_full_source_difference']['scratch_changed']), 95)
        actual = fixture['actual_local_difference']
        self.assertEqual(actual['state_before'], actual['state_after'])
        self.assertEqual(len(actual['changed']), 94)
        self.assertTrue(all(v['before'][:2] == v['after'][:2] and v['before'][4:] == v['after'][4:]
                            for v in actual['changed'].values()))
        # The production rule is verbatim; only its target/input names are reduced.
        (self.out / 'input.o').write_bytes(b'known existing object')
        (self.out / 'build.ninja').write_text(fixture['donor_alink_rule'] +
            '\nbuild new/lib.a: alink input.o\nbuild chrome: phony new/lib.a\n')
        original = arm.output_inventory(self.work / 'src')
        raw = subprocess.run([str(self.ninja), '-C', str(self.out), '-n', 'chrome'], capture_output=True)
        self.assertEqual(raw.returncode, 0)
        self.assertTrue((self.out / 'new').is_dir())
        self.assertNotEqual(original, arm.output_inventory(self.work / 'src'))
        # The existing response file must also survive the isolated preflight byte-for-byte.
        rsp = self.out / 'new/lib.a.rsp'
        rsp.write_bytes(b'original response bytes')
        original = arm.output_inventory(self.work / 'src')
        diagnostics = self.work / 'fingerprint-diagnostics/isolated'
        diagnostics.mkdir()
        with arm.preflight_source(self.work, diagnostics) as (scratch, sandbox):
            self.assertEqual((scratch / 'out/Default/input.o').read_bytes(), b'known existing object')
            result = subprocess.run([*sandbox, str(self.ninja), '-C', str(scratch / 'out/Default'),
                                     '-n', 'chrome'], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((scratch / 'out/Default/new/lib.a.rsp').exists())
        self.assertEqual(original, arm.output_inventory(self.work / 'src'))
        self.assertEqual(rsp.read_bytes(), b'original response bytes')
        report = json.loads((diagnostics / 'preflight-state-difference.json').read_text())
        self.assertEqual(report['original_changed'], {})
        self.assertIn('out/Default/new/lib.a.rsp', report['scratch_changed'])

    def test_scratch_keeps_relative_inputs_and_blocks_external_or_original_writes(self):
        shared = self.work / 'tooling'
        shared.mkdir()
        (shared / 'header.h').write_text('shared input')
        (self.out / 'build.ninja').write_text(
            'rule action\n  command = echo built\n  rspfile = outside/args.rsp\n'
            '  rspfile_content = $in\nbuild result: action ../../../tooling/header.h\n'
            'build chrome: phony result\n')
        (self.out / 'outside').symlink_to(shared, target_is_directory=True)
        diagnostics = self.work / 'fingerprint-diagnostics/isolated'
        diagnostics.mkdir()
        original = arm.output_inventory(self.work / 'src')
        with arm.preflight_source(self.work, diagnostics) as (scratch, sandbox):
            self.assertTrue((scratch / 'out/Default/../../../tooling/header.h').is_file())
            blocked = subprocess.run([*sandbox, str(self.ninja), '-C', str(scratch / 'out/Default'),
                                      '-n', 'chrome'], capture_output=True)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertFalse((shared / 'args.rsp').exists())
            blocked = subprocess.run([*sandbox, '/usr/bin/touch', str(self.out / 'forbidden')], capture_output=True)
            self.assertNotEqual(blocked.returncode, 0)
        self.assertEqual(original, arm.output_inventory(self.work / 'src'))

    def test_response_preflight_passes_then_real_compile_and_object_reuse(self):
        path = self.out / 'build.ninja'
        path.write_text(path.read_text().replace('command = cc -c sample.c -o sample.o',
            'command = cc @sample.rsp -o sample.o\n  rspfile = sample.rsp\n  rspfile_content = -c sample.c'))
        self.assertEqual(self.run_frozen(), 0)
        self.assertTrue((self.out / 'sample.o').is_file())
        obj = arm.output_inventory(self.out)['sample.o']
        self.assertEqual(self.run_frozen(), 0)
        self.assertEqual(obj, arm.output_inventory(self.out)['sample.o'])
        for report in (self.work / 'fingerprint-diagnostics').rglob('preflight-state-difference.json'):
            self.assertEqual(json.loads(report.read_text())['original_changed'], {})

    def test_dynamic_dependency_and_compile_failure_fail_closed(self):
        sample_graph(self.out, extra='dyndep = future.dd\n')
        with self.assertRaisesRegex(ValueError, 'dynamic Ninja'):
            self.run_frozen()
        sample_graph(self.out)
        (self.out / 'sample.c').write_text('this is not valid C;\n')
        self.assertNotEqual(self.run_frozen(), 0)
        self.assertFalse((self.out / 'REGENERATED').exists())
        self.assertFalse((self.out / 'sample.o').exists())


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
