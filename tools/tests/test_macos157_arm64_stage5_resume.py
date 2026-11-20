"""Fixed mac157 stage5 source migration and compile-release route contracts."""
import copy
import gzip
import json
import os
from pathlib import Path
import subprocess
from unittest import mock

import pytest
import yaml

from tools import gen_macos157_arm64_stage5_workflow as workflow
from tools import migrate_macos157_arm64_stage5 as migration
from tools.tests import test_posix_completion as completion

ROOT = Path(__file__).resolve().parents[2]


def test_fixed_profile_and_exact_228_to_231_recipes():
    p = migration.load()
    assert p['run_id'] == 44567777209 and p['job_id'] == 136167335905
    assert p['stage'] == 5 and p['attempt'] == 1
    assert p['artifacts'][0]['id'] == 14626292942
    assert p['artifacts'][0]['size_in_bytes'] == 9560548675
    assert p['artifacts'][0]['digest'] == 'sha256:5e91229895e1d1e17476a9adcb587e7fcd5befb53a9cca027270d16488005cb8'
    assert p['new_selection']['patches'][:-3] == p['old_selection']['patches']
    assert len(p['old_selection']['patches']) == 228
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
                        continuation_stage=1, logical_stage=6, phase='after', state=state)
        path = tmp_path / f'fingerprint-diagnostics/{migration.CONTINUATION}-s1-after-attempt-1.json'
        path.parent.mkdir()
        path.write_text(json.dumps(expected))
        assert migration.verify_checkpoint(tmp_path, 1)['status'] == 'verified'
        for key, value in (('continuation_run', '9'), ('phase', 'before'), ('state', {})):
            path.write_text(json.dumps(dict(expected, **{key: value})))
            with pytest.raises(ValueError, match='same-attempt'):
                migration.verify_checkpoint(tmp_path, 1)


def test_actual_archived_old228_to_new231_private_full_replay(tmp_path):
    fixture = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/macos157_arm64_stage5_source.json.gz').read_bytes()))
    evidence = fixture['actual_migration']
    assert evidence['profile_sha256'] == migration.PROFILE_SHA256
    assert evidence['recipes'] == migration.recipes()
    assert evidence['source_unchanged_outputs'] is True
    assert evidence['out_stream_before'] == evidence['out_stream_after'] == migration.load()['out_stream']
    assert evidence['state'] == migration.load()['build_state']
    assert fixture['archive_verification']['crc_verified'] is True
    assert fixture['archive_verification']['volumes'] == [migration.load()['volume']]
    assert fixture['producer_run']['event'] == 'push'
    assert fixture['producer_job']['id'] == migration.load()['job_id']
    preflight = fixture['actual_ninja_preflight']
    assert preflight['clone.json']['entries'] == 958372
    assert preflight['preflight-state-difference.json']['original_changed'] == {}
    assert preflight['result.json']['protected_graph_proof'] == 'passed'
    assert preflight['protected_files'] == 13717
    assert preflight['result.json']['compile_attempted'] is False
    assert 'Xcode_26.6.app' in preflight['dry_run_stderr']
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


class Client:
    def __init__(self):
        p = migration.load()
        self.run = dict(id=p['run_id'], head_sha=p['head_sha'], run_attempt=1, head_branch='main',
                        event='push', status='completed', conclusion='failure',
                        path='.github/workflows/build-macos-arm64.yml')
        self.job = dict(id=p['job_id'], run_id=p['run_id'], head_sha=p['head_sha'], run_attempt=1,
                        status='completed', conclusion='success', name='macos-arm64 stage 5 (resume compile)')
        self.artifacts = copy.deepcopy(p['artifacts'])

    def get(self, path):
        return self.job if '/jobs/' in path else self.run

    def items(self, path, key):
        return self.artifacts


def test_metadata_rejects_other_runs_jobs_attempts_and_stage6():
    client = Client()
    assert migration.metadata(client) == migration.manifest()
    for container, field, value in (('run', 'run_attempt', 2), ('run', 'head_sha', '0' * 40),
                                    ('job', 'id', 136273081742), ('job', 'conclusion', 'failure'),
                                    ('job', 'name', 'macos-arm64 stage 6 (resume compile)')):
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
    wrapper = yaml.safe_load((ROOT / '.github/workflows/build-mac157-arm64-stage5-resume.yml').read_text())
    inputs = wrapper['jobs']['resume']['with']
    assert inputs['resume_artifact_ids'] == '14626292942'
    assert inputs['macos_compile_release'] is True
    assert inputs['resume_migration'] == 'mac157-arm64-stage5'
    stages = yaml.safe_load(workflow.render())
    assert len(stages['jobs']) == 8
    for job in stages['jobs'].values():
        step = next(s for s in job['steps'] if s.get('id') == 'stage')
        assert workflow.STAGE in step['run']
        assert any('compile-release' in s.get('name', '') for s in job['steps'])
        checkpoints = [s for s in job['steps'] if s.get('id') == 'checkpoint' or
                       s.get('name', '').startswith('Upload tree part ')]
        assert len(checkpoints) == 5
        for checkpoint in checkpoints:
            assert 'always()' in checkpoint['if']
            assert "steps.stage.outcome == 'failure'" in checkpoint['if']
            assert 'mac157_stage5_failed_checkpoint' in checkpoint['if']
            assert 'mac157_failed_checkpoint' in checkpoint['if']
        verification = next(s for s in checkpoints if s.get('id') == 'checkpoint')['run']
        assert 'tools/migrate_macos157_arm64_stage5.py --mode verify' in verification
        assert 'tools/verify_macos_checkpoint.py' in verification
    script = workflow.render_stage()
    subprocess.run(['/bin/bash', '-n'], input=script, text=True, check=True)
    assert 'engine.frozen_ninja(' in (ROOT / 'tools/migrate_macos157_arm64_stage5.py').read_text()
    assert 'norebuild' not in (ROOT / 'tools/migrate_macos157_arm64_stage5.py').read_text()
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
            env.update(CHROMIX_UNCHANGED_RESUME='mac157-arm64-stage5')
            shim = Path(env['PATH'].split(os.pathsep)[0]) / 'python3'
            text = shim.read_text()
            needle = 'if name.startswith("validate_macos156_"):'
            replacement = ('if name == "migrate_macos157_arm64_stage5.py":\n'
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
            root, result, outputs, summary = harness.fixture(arch='arm64', version='157.0.8103.78',
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
                assert 'mac157_stage5_failed_checkpoint' not in outputs
                assert 'upload_snapshot' not in outputs
                assert not (root / 'work/.snapshot-stage-1/p1/tree.tar.zst.001').exists()
            else:
                assert outputs['mac157_stage5_failed_checkpoint'] == 'true'
                assert outputs['upload_snapshot'] == 'true'
                assert (root / 'work/.snapshot-stage-1/p1/tree.tar.zst.001').exists()
    finally:
        harness.doCleanups()
