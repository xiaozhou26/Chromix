"""Fixed mac157 stage6 source migration and compile-release route contracts."""
import copy
import gzip
import json
import os
from pathlib import Path
import subprocess
from unittest import mock

import pytest
import yaml

from tools import gen_macos157_x64_stage6_workflow as workflow
from tools import migrate_macos157_x64_stage6 as migration
from tools.tests import test_posix_completion as completion

ROOT = Path(__file__).resolve().parents[2]


def test_fixed_profile_and_exact_230_to_231_recipes():
    p = migration.load()
    assert p['run_id'] == 44639554770 and p['job_id'] == 136513975353
    assert p['stage'] == 6 and p['attempt'] == 1
    assert p['artifacts'][0]['id'] == 14678246702
    assert p['artifacts'][0]['size_in_bytes'] == 8387231453
    assert p['artifacts'][0]['digest'] == 'sha256:8bf03bc2379f957fb174751bccf956db9b8a64fa539d2852a187fa7652d9d6a3'
    assert p['new_selection']['patches'][:-1] == p['old_selection']['patches']
    assert len(p['old_selection']['patches']) == 230
    assert len(p['new_selection']['patches']) == 231
    assert migration.recipes()
    assert set(p['source_hashes']) == set(migration.SURFACE)
    assert all(v['before'] != v['after'] for v in p['source_hashes'].values())
    for platform, count in (('linux', 233), ('windows', 235)):
        assert len(migration.arp.patch_selection.select(ROOT, platform)[1]) == count


def test_target_recipe_rejects_arbitrary_build_and_tool_hashes():
    original = migration.engine.read
    for name in ('build/macos/build.sh', 'tools/verify_macos_checkpoint.py'):
        def changed(root, path):
            raw = original(root, path)
            return raw + b'\n' if path == name else raw
        with mock.patch.object(migration.engine, 'read', side_effect=changed), pytest.raises(ValueError, match='unreviewed'):
            migration.recipes()


def test_failed_checkpoint_requires_same_attempt_current_after_gate(tmp_path):
    state = migration.load()['build_state']
    with mock.patch.dict(os.environ, GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1'), \
            mock.patch.object(migration, 'source_state', return_value=state):
        expected = dict(migration.identity(), continuation_run='123', continuation_attempt='1',
                        continuation_stage=1, logical_stage=7, phase='after', state=state)
        path = tmp_path / f'fingerprint-diagnostics/{migration.CONTINUATION}-s1-after-attempt-1.json'
        path.parent.mkdir()
        path.write_text(json.dumps(expected))
        assert migration.verify_checkpoint(tmp_path, 1)['status'] == 'verified'
        for key, value in (('continuation_run', '9'), ('phase', 'before'), ('state', {})):
            path.write_text(json.dumps(dict(expected, **{key: value})))
            with pytest.raises(ValueError, match='same-attempt'):
                migration.verify_checkpoint(tmp_path, 1)


@pytest.mark.parametrize('stage', [1, 2, 8])
def test_exact_logical_stage_and_previous_attempt_handoff(tmp_path, stage):
    state = migration.load()['build_state']
    directory = tmp_path / 'fingerprint-diagnostics'
    directory.mkdir()
    (tmp_path / migration.RECEIPT).write_text(json.dumps({'state': state}))
    if stage > 1:
        prior = dict(migration.identity(), continuation_run='123', continuation_attempt='1',
                     continuation_stage=stage - 1, logical_stage=6 + stage - 1, phase='after', state=state)
        (directory / f'{migration.CONTINUATION}-s{stage - 1}-after-attempt-1.json').write_text(json.dumps(prior))
    with mock.patch.dict(os.environ, GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='2',
                         CHROMIX_PREVIOUS_ATTEMPT='1', CHROMIX_BUILD_PROFILE='fast', CHROMIX_USE_UPSTREAM_CACHE='1'), \
            mock.patch.object(migration, 'source_state', return_value=state):
        result = migration.continuation(tmp_path, stage, 'before')
        assert result['logical_stage'] == 6 + stage
        assert result['continuation_attempt'] == '2'
        migration.continuation(tmp_path, stage, 'after')
        assert migration.verify_checkpoint(tmp_path, stage)['logical_stage'] == 6 + stage
        if stage > 1:
            os.environ['CHROMIX_PREVIOUS_ATTEMPT'] = '2'
            with pytest.raises(ValueError, match='producer attempt'):
                migration.continuation(tmp_path, stage, 'before')


def test_actual_archived_old230_to_new231_private_full_replay(tmp_path):
    fixture = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/macos157_x64_stage6_source.json.gz').read_bytes()))
    evidence = fixture['actual_migration']
    assert evidence['profile_sha256'] == migration.PROFILE_SHA256
    assert evidence['recipes'] == migration.recipes()
    assert evidence['source_unchanged_outputs'] is True
    assert evidence['out_stream_before'] == evidence['out_stream_after'] == migration.load()['out_stream']
    assert evidence['state'] == migration.load()['build_state']
    assert fixture['archive_verification']['crc_verified'] is True
    assert fixture['archive_verification']['volumes'] == [migration.load()['volume']]
    assert fixture['producer_run']['event'] == 'workflow_dispatch'
    assert fixture['producer_job']['id'] == migration.load()['job_id']
    assert fixture['extraction']['scope'] == 'complete archive'
    assert fixture['extraction']['source_outputs'] == 170
    assert fixture['extraction']['source_files_including_version'] == 171
    assert fixture['actual_native_check']['accepted'] is False
    assert fixture['actual_native_check']['host_arch'] == 'arm64'
    work, previous = tmp_path / 'work', tmp_path / 'previous'
    src = work / 'src'
    for name, text in fixture['source_files'].items():
        if text is not None:
            path = src / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    for name, text in fixture['tooling'].items():
        path = work / 'tooling/ungoogled-chromium' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (work / 'tooling/ungoogled-chromium-macos').mkdir()
    subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout', str(ROOT), str(previous)], check=True)
    subprocess.run(['git', '-C', str(previous), 'checkout', '--quiet', migration.load()['head_sha']], check=True)
    before = migration.engine.output_inventory(src)
    originals, changes, marker, verified = migration.source_plan(work, previous)
    assert migration.engine.output_inventory(src) == before
    assert set(changes) == set(migration.SURFACE)
    assert verified['patch_count'] == 231
    assert migration.digest(migration.arp._json(verified)) == migration.load()['new_source_report_sha256']
    assert marker['identity'] == verified['identity']
    for name, raw in changes.items():
        pair = migration.load()['source_hashes'][name]
        assert migration.digest(originals[name][0]) == pair['before']
        assert migration.digest(raw) == pair['after']
    unrelated = src / 'base/uxr_config.cc'
    assert unrelated.exists()
    unrelated.write_bytes(unrelated.read_bytes() + b'\n// unexpected source drift\n')
    with pytest.raises(migration.arp.ApplyError, match='completed source changed'):
        migration.source_plan(work, previous)


def test_real_intel_headers_match_known_linux_translation_unit_fix():
    fixture = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/macos157_x64_stage6_source.json.gz').read_bytes()))
    review = json.loads((ROOT / 'patches/chromium157-verification/review.json').read_bytes())['ua_header_evidence']
    native = review['native_compile']
    name = migration.SURFACE[0]
    source = fixture['source_files'][name].encode()
    header = fixture['source_files']['base/version.h'].encode()
    assert native['before_returncode'] == 1 and native['after_returncode'] == 0
    assert native['added_include_flags'] == []
    assert migration.digest(header) == native['files']['base/version.h']
    assert migration.digest(source) == review['files']['linux'][name]['pre_sha256']
    assert migration.load()['source_hashes'][name]['after'] == native['files'][name]
    assert b'class BASE_EXPORT Version {' in header
    assert 'class Version;' in fixture['source_files']['base/version_info/version_info.h']
    assert b'#include "base/version.h"' not in source
    command = fixture['actual_target_command']
    assert migration.digest(command['command'].encode()) == command['sha256']
    assert '../../' + name in command['command']
    assert 'x86_64-apple-macos' in command['command']
    assert command['compile_attempted'] is False
    assert fixture['full_graph_files'] == 18288


def test_native_guard_rejects_arm_host_without_running_donor_tools(tmp_path):
    (tmp_path / 'upstream-cache-preparation.json').write_text('{}')
    with mock.patch.object(migration.engine.prepared, 'host_identity', return_value=('macos', 'arm64')), \
            mock.patch.object(migration.engine.prepared, 'inspect_native_tools') as inspect:
        with pytest.raises(ValueError, match='native macOS Intel required'):
            migration.native_state(tmp_path)
        inspect.assert_not_called()


class Client:
    def __init__(self):
        p = migration.load()
        self.run = dict(id=p['run_id'], head_sha=p['head_sha'], run_attempt=1, head_branch='main',
                        event='workflow_dispatch', status='completed', conclusion='failure',
                        path='.github/workflows/build-macos-x64.yml')
        self.job = dict(id=p['job_id'], run_id=p['run_id'], head_sha=p['head_sha'], run_attempt=1,
                        status='completed', conclusion='success', name='macos-x64 stage 6 (resume compile)')
        self.artifacts = copy.deepcopy(p['artifacts'])

    def get(self, path):
        return self.job if '/jobs/' in path else self.run

    def items(self, path, key):
        return self.artifacts


def test_metadata_rejects_other_runs_jobs_attempts_and_stage7():
    client = Client()
    assert migration.metadata(client) == migration.manifest()
    for container, field, value in (('run', 'run_attempt', 2), ('run', 'head_sha', '0' * 40),
                                    ('job', 'id', 136631456620), ('job', 'conclusion', 'failure'),
                                    ('job', 'name', 'macos-x64 stage 7 (resume compile)')):
        bad = copy.deepcopy(client)
        getattr(bad, container)[field] = value
        with pytest.raises(ValueError):
            migration.metadata(bad)
    for field, value in (('id', 1), ('expired', True), ('digest', 'sha256:' + '0' * 64)):
        bad = copy.deepcopy(client)
        bad.artifacts[0][field] = value
        with pytest.raises(ValueError):
            migration.metadata(bad)


def test_stream_inventory_detects_bytes_and_does_not_follow_links(tmp_path):
    (tmp_path / 'obj').mkdir()
    target = tmp_path / 'obj/compiled.o'
    target.write_bytes(b'compiled output')
    (tmp_path / 'external').symlink_to('/nonexistent/sdk')
    before = migration.stream_inventory(tmp_path)
    info = target.stat()
    target.write_bytes(b'changed! output')
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert migration.stream_inventory(tmp_path) != before
    assert before['entries'] == 2


def test_download_evidence_requires_actual_original_compressed_stream(tmp_path):
    p, expected = migration.load(), migration.manifest()
    origin, report = tmp_path / 'origin.json', tmp_path / 'download.json'
    origin.write_text(json.dumps(expected))
    artifact = dict(p['artifacts'][0], attempts=[dict(status='success',
                    size_in_bytes=p['artifacts'][0]['size_in_bytes'], sha256=p['artifacts'][0]['digest'].split(':')[1])])
    data = dict(status='success', phase='complete', publication='published',
                **{k: p[k] for k in ('repository', 'head_sha', 'run_id')}, artifacts=[artifact],
                volumes=[p['volume']], total_size_in_bytes=p['volume']['size_in_bytes'])
    report.write_text(json.dumps(data))
    assert migration.download_identity(origin, report)['volume'] == p['volume']
    for mutate in (lambda d: d['volumes'][0].update(sha256='0' * 64),
                   lambda d: d['artifacts'][0]['attempts'][0].update(sha256='0' * 64),
                   lambda d: d.update(publication='not_published')):
        bad = copy.deepcopy(data)
        mutate(bad)
        report.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            migration.download_identity(origin, report)


def test_generated_workflow_and_isolated_stage_are_current():
    assert (ROOT / workflow.OUT).read_text() == workflow.render()
    assert (ROOT / workflow.STAGE).read_text() == workflow.render_stage()
    wrapper = yaml.safe_load((ROOT / '.github/workflows/build-mac157-x64-stage6-resume.yml').read_text())
    inputs = wrapper['jobs']['resume']['with']
    assert inputs['resume_artifact_ids'] == '14678246702'
    assert inputs['macos_compile_release'] is True
    assert inputs['resume_migration'] == 'mac157-x64-stage6'
    stages = yaml.safe_load(workflow.render())
    assert len(stages['jobs']) == 8
    for job in stages['jobs'].values():
        step = next(s for s in job['steps'] if s.get('id') == 'stage')
        assert workflow.STAGE in step['run']
        assert any('compile-release' in s.get('name', '') for s in job['steps'])
        route = next(s for s in job['steps'] if s.get('name') == 'Require exact mac157 Intel stage6 route')
        assert 'macos:x64:macos-26-intel' in route['run']
        assert 'mac157-x64-stage6:44639554770:6:1:14678246702' in route['run']
        checkpoints = [s for s in job['steps'] if s.get('id') == 'checkpoint' or
                       s.get('name', '').startswith('Upload tree part ')]
        assert len(checkpoints) == 5
        for checkpoint in checkpoints:
            assert 'always()' in checkpoint['if']
            assert "steps.stage.outcome == 'failure'" in checkpoint['if']
            assert 'mac157_stage6_failed_checkpoint' in checkpoint['if']
            assert 'mac157_failed_checkpoint' in checkpoint['if']
        verification = next(s for s in checkpoints if s.get('id') == 'checkpoint')['run']
        assert 'tools/migrate_macos157_x64_stage6.py --mode verify' in verification
        assert 'tools/verify_macos_checkpoint.py' in verification
    script = workflow.render_stage()
    subprocess.run(['/bin/bash', '-n'], input=script, text=True, check=True)
    assert 'engine.frozen_ninja(' in (ROOT / 'tools/migrate_macos157_x64_stage6.py').read_text()
    assert 'norebuild' not in (ROOT / 'tools/migrate_macos157_x64_stage6.py').read_text()
    assert script.index('unchanged_check after\nif [ "$RC" -ne 0 ]') < script.index('emit upload_snapshot true', script.index('unchanged_check after\nif [ "$RC" -ne 0 ]'))


@pytest.mark.parametrize('failure,damage,success', [
    ('smoke', None, True), ('fingerprint', None, True), ('dependencies', None, True),
    ('none', 'source', False), ('none', 'architecture', False), ('none', 'sha', False),
    ('source_after', None, False), ('archive_after', None, False), ('none', 'compile', False),
    ('checkpoint', 'compile', False), ('snapshot', 'compile', False),
])
def test_dedicated_route_runtime_nonblocking_integrity_hard(failure, damage, success):
    harness = completion.MacOSCompileReleaseTest()
    original = subprocess.run

    def run(args, *pos, **kwargs):
        if isinstance(args, list) and len(args) > 1 and str(args[1]).endswith('/build/posix/ci-stage.sh'):
            stage = Path(args[1])
            stage.write_text(workflow.render_stage())
            repo = stage.parents[2]
            parts = repo / 'build/posix/ci-parts.sh'
            parts.write_text('#!/bin/sh\nexit 23\n' if failure == 'snapshot' else
                             '#!/bin/sh\nmkdir -p "$2/p1"\nprintf snapshot > "$2/p1/tree.tar.zst.001"\n')
            env = kwargs['env']
            env.update(CHROMIX_UNCHANGED_RESUME='mac157-x64-stage6')
            shim = Path(env['PATH'].split(os.pathsep)[0]) / 'python3'
            text = shim.read_text()
            needle = 'if name.startswith("validate_macos156_"):'
            replacement = ('if name == "migrate_macos157_x64_stage6.py":\n'
                           '    if args[args.index("--mode") + 1] == "verify" and failure == "checkpoint":\n'
                           '        sys.exit(31)\n'
                           '    sys.exit(9 if "--mode" in args and args[args.index("--mode") + 1] == "compile" '
                           'and os.environ["FIXTURE_DAMAGE"] == "compile" else 0)\n'
                           'if name.startswith("validate_macos156_"):')
            assert needle in text
            shim.write_text(text.replace(needle, replacement))
        return original(args, *pos, **kwargs)

    try:
        with mock.patch('subprocess.run', side_effect=run):
            root, result, outputs, summary = harness.fixture(arch='x64', version='157.0.8103.78',
                                                            failure=failure, damage=damage)
        assert (result.returncode == 0) == success, result.stdout + result.stderr
        if success:
            assert outputs['finished'] == 'true'
            assert outputs['release_policy'] == 'macos-compile-release'
            assert outputs['runtime_verified'] == 'false'
            assert summary['status'] == 'failed'
        else:
            assert outputs['finished'] == 'false'
        if damage == 'compile':
            assert result.returncode == 9
            assert outputs['status'] == 'failed'
            assert 'mac157_failed_checkpoint' not in outputs
            if failure in ('checkpoint', 'snapshot'):
                assert 'mac157_stage6_failed_checkpoint' not in outputs
                assert 'upload_snapshot' not in outputs
                assert not (root / 'work/.snapshot-stage-1/p1/tree.tar.zst.001').exists()
            else:
                assert outputs['mac157_stage6_failed_checkpoint'] == 'true'
                assert outputs['upload_snapshot'] == 'true'
                assert (root / 'work/.snapshot-stage-1/p1/tree.tar.zst.001').exists()
    finally:
        harness.doCleanups()
