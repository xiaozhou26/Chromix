"""Real complete-source migration; host SDK and the 30GB output tree are not synthetic acceptance."""
import gzip
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from tools import migrate_macos156_hevc_snapshot as migration
from tools import validate_macos156_arm64_stage3_checkpoint as donor
from tools import validate_macos156_x64_stage8_checkpoint as intel
from tools.tests.macos156_historical import recipe_repo
from tools.tests.test_macos156_arm64_stage3_resume import downloaded

ROOT = Path(__file__).resolve().parents[2]
SOURCE = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/macos156_hevc_source.json.gz').read_bytes()))
DONOR = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/macos156_arm64_stage3_receipts.json.gz').read_bytes()))
PREP = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/macos156_arm64_stage8_receipts.json.gz').read_bytes()))
arm = donor.arm


class MigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.previous = Path(cls.temporary.name).resolve() / 'previous'
        subprocess.run(['git', 'clone', '-q', '--shared', '--no-checkout', str(ROOT), str(cls.previous)], check=True)
        subprocess.run(['git', '-C', str(cls.previous), 'checkout', '-q', donor.SHA], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve() / 'work'
        self.src = self.work / 'src'
        files = {'src/' + name: raw for name, raw in SOURCE['files'].items()}
        files.update(DONOR['files'])
        files.update({'tooling/ungoogled-chromium/' + name: raw for name, raw in SOURCE['tooling'].items()})
        for name in ('upstream-cache-preparation.json', 'upstream-cache-restore.json',
                     'src/.chromix-upstream-restored.json', 'src/out/Default/args.gn'):
            files[name] = PREP['files'][name]
        files['src/.chromix-restored-build-prepared.json'] = files['upstream-cache-preparation.json']
        files['src/.chromix-source-ready'] = arm.KEY + '\n'
        files['src/.chromix-toolchain-ready'] = ''
        files['src/out/Default/build.ninja'] = '# protected graph\n'
        files['src/out/Default/.ninja_log'] = '# ninja log v7\n'
        files['src/out/Default/.ninja_deps'] = '# ninjadeps\n\x04\x00\x00\x00'
        files['src/out/Default/obj/unrelated.o'] = 'protected object bytes'
        for name, raw in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(raw)
        (self.work / 'tooling/ungoogled-chromium-macos').mkdir()
        self.origin, self.download = self.work.parent / 'origin.json', self.work.parent / 'download.json'
        self.origin.write_text(json.dumps(donor.expected_manifest()))
        self.download.write_text(json.dumps(downloaded()))
        self.state = json.loads(DONOR['files'][donor.HANDOFF])['state']
        self.enterContext(patch.dict(os.environ, GITHUB_RUN_ID='999', GITHUB_RUN_ATTEMPT='1',
                                    CHROMIX_BUILD_PROFILE='fast', CHROMIX_USE_UPSTREAM_CACHE='1', CHROMIX_JOBS='2'))

    def migrate(self):
        # The full source plan/publication is real; synthetic out cannot attest to native SDK/cache.
        with patch.object(arm, 'source_state', return_value=self.state), patch.object(arm, 'native_state'), \
                patch.object(donor, 'compiled_browser'), patch.object(migration.shared, '_verify_tooling'), \
                patch.object(migration.shared.restore, 'verify_restored'):
            return migration.migrate(self.work, self.previous, ROOT, self.origin, self.download)

    def test_real_full_reverse_forward_atomic_publication_and_protected_outputs(self):
        out = self.src / 'out/Default'
        before = arm.output_inventory(out)
        prep = arm.read(self.src, '.chromix-restored-build-prepared.json')
        result = self.migrate()
        self.assertEqual(result['changed_files'], sorted(migration.SURFACE))
        self.assertEqual((result['old_patch_count'], result['patch_count']), (226, 228))
        self.assertEqual(arm.output_inventory(out), before)
        self.assertEqual(arm.read(self.src, '.chromix-restored-build-prepared.json'), prep)
        self.assertEqual(arm.read(self.work, migration.ARCHIVED_SOURCE), DONOR['files']['fingerprint-diagnostics/source-final.json'].encode())
        for name, (_, digest) in migration.SURFACE.items():
            self.assertEqual(arm.sha(arm.read(self.src, name)), digest)
        self.assertEqual(arm.read(self.src, migration.shared.READY), (migration.KEY + '\n').encode())
        self.assertFalse((self.src / migration.shared.TRANSACTION).exists())
        self.assertFalse((self.src / migration.arp.IN_PROGRESS).exists())
        migration.live_source(ROOT, self.work, self.work / 'new-source.json')
        with self.assertRaises(ValueError):
            self.migrate()
        # State/graph fixtures are synthetic; production compares the exact donor graph.
        original = arm.identity_hash
        with patch.object(arm, 'identity_hash', wraps=original) as digest:
            digest.side_effect = lambda v: self.state['graph'] if isinstance(v, dict) and set(v) == {'out/Default/build.ninja'} else original(v)
            state = migration.source_state(ROOT, self.work)
        self.assertEqual(state['graph'], self.state['graph'])
        from tools import fingerprint_acceptance as acceptance
        selected, patches = migration.arp.patch_selection.select(ROOT, 'macos')
        acceptance.check_source(self.work / 'fingerprint-diagnostics/source-final.json', self.src, selected)
        with patch.object(migration, 'source_state', return_value=self.state), patch.object(arm, 'native_state'):
            migration.continuation(ROOT, self.work, 1, 'before')
            with patch.object(arm.restore_ninja, 'host_identity', return_value=('macos', 'arm64')), \
                    patch.object(arm.restore_ninja, 'binary_architectures', return_value={'arm64'}), \
                    patch.object(arm.restore_ninja, 'probe_version', return_value='1.13.2'), \
                    patch.object(migration, 'frozen_ninja', return_value=0) as compile_call:
                self.assertEqual(migration.compile_tree(ROOT, self.work, 1), 0)
                compile_call.assert_called_once()
            migration.continuation(ROOT, self.work, 1, 'after')
            parts = self.work.parent / 'parts'
            subprocess.run(['/bin/bash', str(ROOT / 'build/posix/ci-parts.sh'), str(self.work), str(parts)],
                           env=dict(os.environ, COPYFILE_DISABLE='1'), capture_output=True, check=True)
            restored = self.work.parent / 'restored'
            subprocess.run([os.sys.executable, str(ROOT / 'tools/restore_posix_snapshot.py'), str(parts), str(restored)],
                           capture_output=True, check=True)
            with patch.dict(os.environ, GITHUB_RUN_ATTEMPT='2', CHROMIX_PREVIOUS_ATTEMPT='1'):
                before = migration.continuation(ROOT, restored, 2, 'before')
                self.assertEqual(before['logical_stage'], 14)
            self.assertEqual(arm.read(restored, donor.FAILURE), arm.read(self.work, donor.FAILURE))
            with patch.dict(os.environ, GITHUB_RUN_ID='1000'), self.assertRaises(ValueError):
                migration.continuation(ROOT, restored, 3, 'before')

    def test_failed_second_publication_leaves_blockers_and_no_admission(self):
        original = migration.arp._atomic_write
        def write(path, *args, **kwargs):
            if path == self.src / list(migration.SURFACE)[1]:
                raise OSError('injected second-file failure')
            return original(path, *args, **kwargs)
        with patch.object(migration.arp, '_atomic_write', side_effect=write), self.assertRaises(OSError):
            self.migrate()
        self.assertTrue((self.src / migration.shared.TRANSACTION).is_file())
        self.assertTrue((self.src / migration.arp.IN_PROGRESS).is_file())
        self.assertFalse((self.work / migration.RECEIPT).exists())
        self.assertEqual(arm.read(self.src, migration.shared.READY), (arm.KEY + '\n').encode())

    def test_wrong_preimage_full_file_or_historical_receipt_rejected_read_only(self):
        name = next(iter(migration.SURFACE))
        target = self.src / name
        raw = target.read_bytes()
        target.write_bytes(raw + b'\n')
        before = arm.output_inventory(self.src)
        with self.assertRaises(ValueError):
            migration.source_plan(self.work, self.previous, ROOT)
        self.assertEqual(arm.output_inventory(self.src), before)
        target.write_bytes(raw)
        target = self.work / donor.HANDOFF
        target.write_bytes(target.read_bytes() + b' ')
        with self.assertRaises(ValueError):
            self.migrate()
        self.assertFalse((self.src / migration.shared.TRANSACTION).exists())

    def test_old_helpers_reject_228_while_historical_226_stays_fixed(self):
        for helper in (arm, intel):
            with self.assertRaises(ValueError):
                helper.recipes(ROOT)
            self.assertTrue(helper.recipes(recipe_repo()))
        with self.assertRaises(ValueError):
            donor.samebrowser_recipes(ROOT)
        self.assertEqual(len(migration.arp.patch_selection.select(ROOT, 'macos')[1]), 228)
        for platform, count in [('linux', 226), ('windows', 232)]:
            self.assertEqual(len(migration.arp.patch_selection.select(ROOT, platform)[1]), count)
        migration.recipes(ROOT, self.previous)

    def test_exact_recipe_rejects_source_patch_gn_and_probe_changes(self):
        original = arm.read
        for name in ['build/args.macos.gn', 'tools/fingerprint_backend_probe.js',
                     'tools/patch_selection.py', 'build/patch-selection-156.json', *migration.PATCHES]:
            def read(root, path):
                raw = original(root, path)
                return raw + b'\n' if root == ROOT and path == name else raw
            with self.subTest(name=name), patch.object(arm, 'read', side_effect=read), self.assertRaises(ValueError):
                migration.recipes(ROOT, self.previous)

    def test_compile_plan_allows_only_two_reviewed_source_objects(self):
        commands = [f'clang++ -c ../../{name} -o obj/repaired.o' for name in migration.SURFACE]
        migration.check_compile_plan(commands, self.src)
        migration.check_compile_plan(commands + ['rm -f obj/lib.a && llvm-ar -T -s -r -c -s -D obj/lib.a @obj/lib.a.rsp'], self.src)
        migration.check_compile_plan(['clang++ -shared -o ChromiumFramework @framework.rsp'], self.src)
        migration.check_compile_plan([], self.src)
        for bad in (commands + [commands[0]], ['clang++ -c ../../other.cc -o obj/unrelated.o']):
            with self.assertRaises(ValueError):
                migration.check_compile_plan(bad, self.src)

    def test_source_migration_mode_refuses_cold_tree(self):
        work = self.work.parent / 'empty'
        result = subprocess.run(['/bin/bash', str(ROOT / 'build/posix/ci-stage.sh'),
                                 '--platform', 'macos', '--arch', 'arm64', '--workdir', str(work)],
                                env=dict(os.environ, CHROMIX_UNCHANGED_RESUME='mac156-arm64-hevc',
                                         PATH=str(Path(os.sys.executable).parent) + os.pathsep + os.environ['PATH']),
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unchanged mac156 continuation identity failed', result.stderr)
        self.assertFalse((work / 'src').exists())

    def test_workflow_uses_source_migration_and_preserves_full_runtime_gates(self):
        entry = yaml.safe_load((ROOT / '.github/workflows/build-mac156-arm64-stage3-resume.yml').read_text())
        self.assertEqual(entry['jobs']['resume']['with']['resume_migration'], 'mac156-arm64-hevc')
        workflow = yaml.safe_load((ROOT / '.github/workflows/build-posix-github.yml').read_text())
        for i in range(1, 9):
            steps = workflow['jobs'][f'posix-{i}']['steps']
            self.assertTrue(any('CHROMIX_UNCHANGED_RESUME=mac156-arm64-hevc' in s.get('run', '') for s in steps))
        stage = (ROOT / 'build/posix/ci-stage.sh').read_text()
        old = subprocess.check_output(['git', '-C', str(ROOT), 'show', 'f6da88f:build/posix/ci-stage.sh'], text=True)
        marker = '# ---- completion checks'
        self.assertEqual(stage[stage.index(marker):], old[old.index(marker):])


if __name__ == '__main__':
    unittest.main()
