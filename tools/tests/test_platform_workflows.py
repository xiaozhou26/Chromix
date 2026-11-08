"""Platform entrypoints must dispatch and fail independently."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

REPO = Path(__file__).resolve().parents[2]
DIRECTORY = REPO / '.github/workflows'
PLATFORMS = {
    'build-linux-x64': ('linux', 'x64', 'ubuntu-22.04', 'chromix-linux-x64'),
    'build-linux-arm64': ('linux', 'arm64', None, 'chromix-linux-arm64'),
    'build-macos-x64': ('macos', 'x64', 'macos-15-intel', 'chromix-mac-x64'),
    'build-macos-arm64': ('macos', 'arm64', 'macos-15', 'chromix-mac-arm64'),
}


def load(name):
    return yaml.safe_load((DIRECTORY / (name + '.yml')).read_text())


def events(workflow):
    return workflow.get('on', workflow.get(True))


class PlatformWorkflowTest(unittest.TestCase):
    def test_exactly_six_build_entrypoints(self):
        names = {load(path.stem)['name'] for path in DIRECTORY.glob('build-*.yml')
                 if 'workflow_dispatch' in events(load(path.stem))}
        self.assertEqual(names, set(PLATFORMS) | {'build-win-x64-github', 'build-win-arm64-github'})
        self.assertFalse((DIRECTORY / 'build-cross-platform.yml').exists())
        self.assertEqual(set(events(load('build-posix-github'))), {'workflow_call'})

    def test_posix_entries_keep_platform_inputs_and_isolation(self):
        groups = set()
        for name, (platform, arch, runner, artifact) in PLATFORMS.items():
            with self.subTest(name=name):
                workflow = load(name)
                self.assertEqual(workflow['name'], name)
                self.assertEqual(set(events(workflow)), {'push', 'workflow_dispatch'})
                self.assertEqual(events(workflow)['push']['branches'], ['main'])
                dispatch = events(workflow)['workflow_dispatch']['inputs']
                self.assertTrue(dispatch['use_upstream_cache']['default'])
                modes = ['staged', 'single', 'verify'] if name == 'build-linux-arm64' else ['staged', 'single']
                for key, choices, default in (
                    ('build_profile', ['fast', 'release'], 'fast'),
                    ('build_mode', modes, 'staged'),
                ):
                    self.assertEqual(dispatch[key]['type'], 'choice')
                    self.assertEqual(dispatch[key]['options'], choices)
                    self.assertEqual(dispatch[key]['default'], default)
                self.assertFalse(workflow['concurrency']['cancel-in-progress'])
                groups.add(workflow['concurrency']['group'])
                expected_jobs = {'build', 'reverify'} if name == 'build-linux-arm64' else {'build'}
                self.assertEqual(set(workflow['jobs']), expected_jobs)
                job = workflow['jobs']['build']
                if name == 'build-linux-arm64':
                    self.assertEqual(job['if'], "${{ inputs.build_mode != 'verify' }}")
                    reverify = workflow['jobs']['reverify']
                    self.assertEqual(reverify['if'],
                                     "${{ github.event_name == 'workflow_dispatch' && inputs.build_mode == 'verify' }}")
                    self.assertEqual(reverify['runs-on'], 'ubuntu-24.04-arm')
                    self.assertNotIn('needs', reverify)
                self.assertNotIn('needs', job)
                self.assertNotIn('strategy', job)
                self.assertEqual(job['uses'], './.github/workflows/build-posix-github.yml')
                self.assertEqual(job['secrets'], 'inherit')
                inputs = job['with']
                self.assertEqual((inputs['platform'], inputs['arch'], inputs['artifact']),
                                 (platform, arch, artifact))
                self.assertEqual(inputs['max-stages'], "${{ inputs.build_mode == 'single' && 1 || 8 }}")
                self.assertEqual(inputs['build_profile'], "${{ inputs.build_profile || 'fast' }}")
                self.assertIn("github.event_name != 'workflow_dispatch' || inputs.use_upstream_cache",
                              inputs['use_upstream_cache'])
                if runner:
                    self.assertEqual(inputs['runner'], runner)
                paths = events(workflow)['push']['paths']
                self.assertIn(f'.github/workflows/{name}.yml', paths)
                self.assertIn('build/*', paths)
                self.assertNotIn('build/**', paths)
                self.assertIn(f'build/{platform}/**', paths)
                self.assertNotIn('build/windows/**', paths)
        self.assertEqual(len(groups), 4)
        self.assertNotIn(load('build-win-x64-github')['concurrency']['group'], groups)

    def test_windows_push_prefers_cache_and_keeps_manual_resume(self):
        workflow = load('build-win-x64-github')
        self.assertIn('push', events(workflow))
        self.assertEqual(events(workflow)['push']['branches'], ['main'])
        self.assertFalse(workflow['concurrency']['cancel-in-progress'])
        self.assertEqual(workflow['env']['CHROMIX_PREFER_UPSTREAM_CACHE'],
                         "${{ github.event_name == 'push' && '1' || '0' }}")
        self.assertEqual(workflow['env']['CHROMIX_USE_UPSTREAM_CACHE'],
                         "${{ (inputs.use_upstream_cache || inputs.upstream_run_id != '') && '1' || '0' }}")
        stage = next(step for step in workflow['jobs']['build-1']['steps'] if step.get('id') == 'stage')
        self.assertEqual(stage['env']['USE_UPSTREAM_CACHE'], '${{ inputs.use_upstream_cache }}')
        self.assertEqual(stage['env']['UPSTREAM_RUN_ID'], "${{ inputs.upstream_run_id }}")
        self.assertIn('resume_run_id', events(workflow)['workflow_dispatch']['inputs'])
        paths = events(workflow)['push']['paths']
        self.assertIn('build/windows/**', paths)
        self.assertNotIn('build/posix/**', paths)
        self.assertNotIn('build/**', paths)
        self.assertIn('build-12', workflow['jobs'])


class LinuxFailedCheckpointTest(unittest.TestCase):
    def setUp(self):
        self.jobs = load('build-posix-github')['jobs']

    def allowed(self, step, *, platform='linux', outcome='failure', cancelled=False, **outputs):
        expression = step['if'].removeprefix('${{ ').removesuffix(' }}')
        expression = expression.replace('!cancelled()', str(not cancelled))
        expression = expression.replace('inputs.platform', repr(platform))
        expression = expression.replace('steps.stage.outcome', repr(outcome))
        expression = re.sub(r'steps\.([\w_]+)\.outputs\.([\w_]+)',
                            lambda match: repr(outputs.get(match[1] + '.' + match[2], '')),
                            expression)
        expression = expression.replace('steps.checkpoint.outcome',
                                        repr(outputs.get('checkpoint_outcome', '')))
        return eval(expression.replace('&&', 'and').replace('||', 'or'), {'__builtins__': {}})

    def test_all_stages_preserve_compiled_tree_independently_of_runtime_upload(self):
        for number in range(1, 9):
            steps = self.jobs[f'posix-{number}']['steps']
            failed = next(s for s in steps if s.get('name') == 'Upload failed Linux runtime bundle')
            preserve = next(s for s in steps if s.get('id') == 'linux_runtime_checkpoint')
            verify = next(s for s in steps if s.get('id') == 'checkpoint')
            uploads = [s for s in steps if s.get('name', '').startswith('Upload tree part')]
            self.assertEqual(len(uploads), 4)
            self.assertLess(steps.index(failed), steps.index(preserve))
            self.assertLess(steps.index(preserve), steps.index(verify))
            self.assertIn(f'-failed-runtime-s{number}-attempt-', failed['with']['name'])
            self.assertEqual(failed['with']['if-no-files-found'], 'error')
            self.assertEqual(failed['with']['compression-level'], 0)
            for path in ('/dist/${{ inputs.artifact }}.zip', '/dist/SHA256SUMS',
                         '/fingerprint-diagnostics/', f'/chromix-logs/stage-{number}.log'):
                self.assertIn(path, failed['with']['path'])
            self.assertNotIn('success()', preserve['if'])
            self.assertNotIn('runtime_failed', preserve['if'])
            self.assertNotIn('package_ready', preserve['if'])
            for compiled, package, runtime, upload, snapshot in (
                ('true', 'true', 'true', True, True),
                ('true', 'true', '', False, True),
                ('true', '', '', False, True),
                ('', '', '', False, False),
            ):
                state = {'stage.compiled_ready': compiled, 'stage.package_ready': package,
                         'stage.runtime_failed': runtime, 'stage.finished': 'false'}
                self.assertEqual(self.allowed(failed, **state), upload)
                self.assertEqual(self.allowed(preserve, **state), snapshot)
                final = next(s for s in steps if s.get('name') == 'Upload final bundle')
                self.assertFalse(self.allowed(final, **state))
                for disabled in ({'platform': 'macos'}, {'outcome': 'success'}, {'cancelled': True}):
                    self.assertFalse(self.allowed(failed, **state, **disabled))
                    self.assertFalse(self.allowed(preserve, **state, **disabled))
            for step in (failed, preserve, verify, *uploads):
                self.assertNotIn('continue-on-error', step)
            self.assertIn('snapshot_volumes.py', verify['run'])
            self.assertIn('zstd -d -T0 | python3 tools/verify_snapshot_tar.py', verify['run'])
            for index, step in enumerate(uploads, 1):
                self.assertLess(steps.index(verify), steps.index(step))
                self.assertEqual(step['with']['name'], '${{ inputs.artifact }}-tree-s' + str(number)
                                 + '-attempt-${{ github.run_attempt }}-part' + str(index))
                for ready in ('true', ''):
                    for checked in ('success', 'failure', ''):
                        state = {'linux_runtime_checkpoint.upload_snapshot': ready,
                                 'checkpoint_outcome': checked}
                        self.assertEqual(self.allowed(step, **state), ready == 'true' and checked == 'success')
                        self.assertFalse(self.allowed(step, cancelled=True, **state))

    def test_existing_handoff_and_mac_checkpoint_guards_keep_their_semantics(self):
        steps = self.jobs['posix-3']['steps']
        guarded = [s for s in steps if s.get('name', '').startswith('Upload tree part')
                   or s.get('id') == 'checkpoint']
        for step in guarded:
            for platform in ('linux', 'macos'):
                for handoff in ('true', ''):
                    for mac in ('true', ''):
                        for linux in ('true', ''):
                            state = {'stage.upload_snapshot': handoff, 'runtime_checkpoint.upload_snapshot': mac,
                                     'linux_runtime_checkpoint.upload_snapshot': linux, 'checkpoint_outcome': 'success'}
                            expected = handoff == 'true' or mac == 'true' or (platform == 'linux' and linux == 'true')
                            self.assertEqual(self.allowed(step, platform=platform, **state), expected)

    def test_failed_linux_checkpoint_is_accepted_by_unchanged_resume_validator(self):
        from tools.validate_posix_snapshot import validate

        steps = self.jobs['posix-3']['steps']
        sha = 'a' * 40
        run = {'id': 1, 'name': 'build-linux-x64', 'path': '.github/workflows/build-linux-x64.yml',
               'head_branch': 'main', 'event': 'workflow_dispatch', 'status': 'completed',
               'conclusion': 'failure', 'head_sha': sha, 'run_attempt': 1,
               'repository': {'full_name': 'fixture/repo'}, 'head_repository': {'full_name': 'fixture/repo'}}
        artifact = {'id': 2, 'name': 'chromix-linux-x64-tree-s3-attempt-1-part1',
                    'expired': False, 'size_in_bytes': 1024, 'digest': 'sha256:' + 'b' * 64,
                    'workflow_run': {'id': 1, 'head_sha': sha}}
        job = {'id': 3, 'name': 'linux-x64 stage 3 (resume compile)', 'status': 'completed',
               'conclusion': 'failure', 'steps': [
                   {'name': s['name'], 'conclusion': 'failure' if s.get('id') == 'stage' else 'success'}
                   for s in steps if s.get('name')]}

        class Client:
            def get(self, path):
                return run

            def items(self, path, key):
                return [job] if key == 'jobs' else [artifact]

        receipt = validate(Client(), 'fixture/repo', 1, 3, 1, 'x64', [2], platform='linux')
        self.assertEqual(receipt['artifacts'][0]['id'], 2)
        self.assertEqual(receipt['stage'], 3)

    def run_checkpoint(self, root, code=0):
        helper = root / 'build/posix/ci-parts.sh'
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text('#!/bin/sh\n'
                          'test ! -e "$1/smoke" || exit 81\n'
                          'test ! -e "$1/dist/chromix" || exit 82\n'
                          'test ! -e "$1/.snapshot-stage-2" || exit 83\n'
                          'touch "$1/snapshot-called"\n'
                          f'exit {code}\n')
        output = root / 'output'
        output.write_text('')
        script = next(s['run'] for s in self.jobs['posix-3']['steps']
                      if s.get('id') == 'linux_runtime_checkpoint')
        return subprocess.run(['bash', '-c', script], cwd=root, capture_output=True, text=True,
                              env={**os.environ, 'RUNNER_TEMP': str(root), 'GITHUB_OUTPUT': str(output)},
                              timeout=10)

    def test_cleanup_precedes_snapshot_and_preserves_source_objects_caches_and_bundle(self):
        for code in (0, 23):
            with self.subTest(code=code), tempfile.TemporaryDirectory(prefix='linux checkpoint ') as temp:
                root = Path(temp)
                work = root / 'chromix-build'
                keep = ('src/source.cc', 'src/out/Default/chrome', 'src/out/Default/obj/compiled.o',
                        'src/out/Default/.ninja_log', 'src/.chromix-upstream-restored.json',
                        'src/dist/chromix/keep', 'src/smoke/keep', 'src/.snapshot-stage-2/keep',
                        'tooling/helper.py', 'download_cache/archive.tar.xz',
                        'dist/chromix-linux-x64.zip', 'dist/SHA256SUMS',
                        'fingerprint-diagnostics/source-final.json',
                        'fingerprint-diagnostics/runtime-s3-1/acceptance.json')
                remove = ('smoke/profile/cache', 'dist/chromix/chrome', '.snapshot-stage-2/p1/old')
                for name in (*keep, *remove):
                    path = work / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(name)
                (work / 'tooling/download_cache').symlink_to('../download_cache')
                (work / 'smoke/source-link').symlink_to('../src')
                (work / 'dist/chromix/source-link').symlink_to('../../src')
                before = {name: ((work / name).read_bytes(), (work / name).stat().st_mtime_ns) for name in keep}
                result = self.run_checkpoint(root, code)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertEqual((root / 'output').read_text(), '' if code else 'upload_snapshot=true\n')
                self.assertTrue((work / 'snapshot-called').is_file())
                self.assertTrue((work / 'tooling/download_cache').is_symlink())
                for name, state in before.items():
                    self.assertEqual(((work / name).read_bytes(), (work / name).stat().st_mtime_ns), state)
                for name in remove:
                    self.assertFalse((work / name).exists())

    def test_unsafe_cleanup_paths_fail_before_any_deletion_or_snapshot(self):
        for name in ('smoke', 'dist', 'dist/chromix', '.snapshot-stage-2', 'src', '.'):
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix='linux checkpoint unsafe ') as temp:
                root = Path(temp)
                work = root / 'chromix-build'
                (work / 'src').mkdir(parents=True)
                outside = root / 'keep'
                outside.mkdir()
                (outside / 'sentinel').write_text('untouched')
                target = work / name
                if name == '.':
                    (work / 'src').rmdir()
                    work.rmdir()
                elif name == 'src':
                    target.rmdir()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(outside)
                safe_copy = None
                if name not in ('.', 'smoke'):
                    safe_copy = work / 'smoke/keep-until-validation-completes'
                    safe_copy.parent.mkdir()
                    safe_copy.write_text('not deleted')
                result = self.run_checkpoint(root)
                self.assertNotEqual(result.returncode, 0)
                if safe_copy is not None:
                    self.assertEqual(safe_copy.read_text(), 'not deleted')
                self.assertIn('unsafe', result.stderr)
                self.assertEqual((root / 'output').read_text(), '')
                self.assertEqual((outside / 'sentinel').read_text(), 'untouched')
                self.assertTrue(target.is_symlink())
                self.assertFalse((work / 'snapshot-called').exists())

    @unittest.skipUnless(shutil.which('zstd'), 'zstd required')
    def test_real_failed_linux_checkpoint_roundtrip_keeps_complete_compiled_tree(self):
        with tempfile.TemporaryDirectory(prefix='linux compiled roundtrip ') as temp:
            root = Path(temp)
            work = root / 'chromix-build'
            payloads = ('src/source.cc', 'src/out/Default/chrome', 'src/out/Default/obj/compiled.o',
                        'src/out/Default/.ninja_deps', 'src/out/Default/.ninja_log',
                        'src/.chromix-upstream-restored.json', 'tooling/helper.py',
                        'fingerprint-diagnostics/source-final.json')
            for name in (*payloads, 'dist/chromix/chrome', 'smoke/profile/data'):
                path = work / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
                path.chmod(0o755 if name.endswith('/chrome') else 0o644)
            (work / 'tooling/download_cache').symlink_to('../download_cache')
            before = {name: ((work / name).read_bytes(), (work / name).stat().st_mtime_ns,
                             (work / name).stat().st_mode) for name in payloads}
            script = next(s['run'] for s in self.jobs['posix-3']['steps']
                          if s.get('id') == 'linux_runtime_checkpoint')
            env = {**os.environ, 'RUNNER_TEMP': temp, 'GITHUB_OUTPUT': str(root / 'output'),
                   'CHROMIX_SNAPSHOT_VOLUME_BYTES': '65536'}
            result = subprocess.run(['bash', '-c', script], cwd=REPO, env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / 'output').read_text(), 'upload_snapshot=true\n')
            restored = root / 'restored'
            result = subprocess.run([sys.executable, str(REPO / 'tools/restore_posix_snapshot.py'),
                                     str(work / '.snapshot-stage-3'), str(restored)], env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for name, state in before.items():
                self.assertEqual(((restored / name).read_bytes(), (restored / name).stat().st_mtime_ns,
                                  (restored / name).stat().st_mode), state)
            self.assertEqual(os.readlink(restored / 'tooling/download_cache'), '../download_cache')
            self.assertFalse((restored / 'dist').exists())
            self.assertFalse((restored / 'smoke').exists())


if __name__ == '__main__':
    unittest.main()
