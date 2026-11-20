"""Exact stage5 donor admission and source-only Storage transaction contracts."""
from __future__ import annotations

import copy
import difflib
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from tools import linux157_storage as storage
from tools import linux157_source as source
from tools import gen_linux157_arm64_storage_workflow as workflow
from tools import gen_posix_workflow as base
from tools.tests.test_linux157_pre_gn_workflow import put

ROOT = source.ROOT


def test_exact_stage5_identity_and_verified_three_single_file_review():
    m = storage.load()
    want = storage.expected('arm64')
    assert (want['run_id'], want['stage'], want['attempt'], want['job_id']) == (44660206036, 5, 1, 136134564115)
    assert want['head_sha'] == '289d5730627d86acf55f9a1b3770dbd4ecd0e025'
    assert want['artifacts'][0]['id'] == 14608249261
    assert want['artifacts'][0]['size_in_bytes'] == 4855771425
    assert want['artifacts'][0]['digest'] == 'sha256:e561a6a26a3a2682d28a9d869fb635d0dc795ec9e8fe7340986708874e9e8e7d'
    assert m['continuation_stage'] == 6
    assert m['source_final']['patch_count'] == 229
    assert len(m['source_receipt']['chromix_selection']['patches']) == 229
    assert m['output_tree']['entries'] > 10000
    assert m['files']['src/.chromix-linux157-ua-migration.json']
    migration = storage.review(m)
    assert migration['status'] == 'verified' and migration['new_patch_count'] == 232
    assert [Path(p['patch']).name[:4] for p in migration['additions']] == ['0239', '0240', '0241']
    assert all(len(p['targets']) == 1 for p in migration['additions'])


@pytest.mark.parametrize('field', ['run_id', 'stage', 'attempt', 'job_id', 'head_sha', 'arch'])
def test_wrong_tuple_rejects_before_tree_access(tmp_path, field):
    metadata = storage.expected('arm64')
    metadata[field] = 'wrong' if field in ('head_sha', 'arch') else metadata[field] + 1
    with pytest.raises(ValueError, match='tuple'):
        storage.admit(tmp_path, 'arm64', metadata)
    assert not list(tmp_path.iterdir())


def test_pending_slot_cannot_download_or_publish(tmp_path, monkeypatch):
    m = copy.deepcopy(storage.load())
    m['migration']['status'] = 'pending-parent-review'
    monkeypatch.setattr(storage, 'load', lambda *args: m)
    with pytest.raises(ValueError, match='strict parent review'):
        storage.restore(tmp_path / 'absent', 'arm64')
    with pytest.raises(ValueError, match='strict parent review'):
        storage.admit(tmp_path, 'arm64', storage.expected('arm64'))
    assert not list(tmp_path.iterdir())


def metadata_client(attack=None):
    location = os.environ.get('CHROMIX_STORAGE_AUDIT')
    if not location:
        pytest.skip('requires authenticated actual stage5 GitHub metadata')
    root = Path(location)
    run = json.loads((root / 'run.json').read_bytes())
    jobs = json.loads((root / 'jobs.json').read_bytes())['jobs']
    artifacts = json.loads((root / 'artifacts.json').read_bytes())['artifacts']
    job = next(j for j in jobs if j['id'] == 136134564115)
    artifact = next(a for a in artifacts if a['id'] == 14608249261)
    if attack == 'attempt': run['run_attempt'] = 2
    elif attack == 'digest': artifact['digest'] = 'sha256:' + '0' * 64
    elif attack == 'size': artifact['size_in_bytes'] -= 1
    elif attack == 'head': job['head_sha'] = '0' * 40
    elif attack == 'job': job['run_id'] = 1
    elif attack == 'stage': artifact['name'] = artifact['name'].replace('-s5-', '-s4-')
    elif attack == 'arch': artifact['name'] = artifact['name'].replace('-arm64-', '-x64-')
    elif attack == 'preserve': next(s for s in job['steps'] if s['name'] == 'Preserve latest failed Linux source/output checkpoint')['conclusion'] = 'failure'
    elif attack == 'upload': next(s for s in job['steps'] if s['name'] == 'Upload tree part 1')['conclusion'] = 'skipped'
    elif attack == 'workflow': run['path'] = '.github/workflows/build-linux157-arm64-locale-resume.yml'
    elif attack == 'expired': artifact['expired'] = True
    class Client:
        def get(self, path): return run
        def items(self, path, key): return jobs if key == 'jobs' else artifacts
    return Client()


def test_actual_metadata_accepts_exact_latest_stage5():
    assert storage.validate_metadata(metadata_client(), 'arm64') == storage.expected('arm64')


@pytest.mark.parametrize('attack', ['attempt', 'digest', 'size', 'head', 'job', 'stage', 'arch', 'preserve', 'upload', 'workflow', 'expired'])
def test_actual_metadata_mutations_fail_closed(attack):
    with pytest.raises(ValueError):
        storage.validate_metadata(metadata_client(attack), 'arm64')


def patch(name, before, after):
    raw = (f'diff --git a/{name} b/{name}\n' + ''.join(difflib.unified_diff(
        before.splitlines(True), after.splitlines(True), fromfile='a/' + name, tofile='b/' + name))).encode()
    transformed, entries = storage.arp.transform_patch(raw, set(), [])
    return raw, transformed, entries


def fixture(root):
    src = root / 'src'
    src.mkdir()
    stack = []
    for i in range(229):
        name = f'old/{i}.cc'
        put(src / name, f'patched{i}\n')
        raw, transformed, entries = patch(name, f'original{i}\n', f'patched{i}\n')
        stack.append((f'old/{i}.patch', transformed, entries))
    migration = {'status': 'verified', 'old_patch_count': 229, 'new_patch_count': 232, 'additions': [], 'sources': {}}
    for i, names in enumerate((('protocol/Storage.pdl',), ('handlers/storage.cc',), ('handlers/storage.h',))):
        data, targets = b'', []
        for name in names:
            before, after = 'disabled old\n', 'disabled current\n'
            put(src / name, before)
            raw, transformed, entries = patch(name, before, after)
            data += transformed
            targets.extend(entries)
            migration['sources'][name] = {'before': source.digest(before.encode()), 'after': source.digest(after.encode())}
        name = f'patches/chromium157-additions/fixture-{i}.patch'
        migration['additions'].append({'patch': name, 'sha256': source.digest(data), 'targets': list(names)})
        stack.append((name, data, targets))
    put(src / 'out/Chromix/gen/protocol/storage.h', 'keep generated pure virtual\n')
    put(src / 'out/Chromix/args.gn', 'chrome_pgo_phase = 0\n')
    put(src / 'out/Chromix/keep.o', 'compiled ARM object\n')
    return src, stack, {'migration': migration}


def test_fixture_full229_plus_three_single_file_patches_private_roundtrip(tmp_path):
    src, stack, manifest = fixture(tmp_path)
    output = storage.snapshot_outputs(src / 'out')
    before, after = storage.private_migration(src, stack, tmp_path, manifest)
    changed = {name for name in after if before[name][0] != after[name]}
    assert changed == set(manifest['migration']['sources'])
    for name, (raw, _) in before.items():
        assert (src / name).read_bytes() == raw
    assert storage.snapshot_outputs(src / 'out') == output
    for name, pin in manifest['migration']['sources'].items():
        assert source.digest(before[name][0]) == pin['before']
        assert source.digest(after[name]) == pin['after']


@pytest.mark.parametrize('attack', ['count', 'extra-target', 'generated', 'old-source', 'symlink'])
def test_fixture_private_replay_fail_closed(tmp_path, attack):
    src, stack, manifest = fixture(tmp_path)
    output = storage.snapshot_outputs(src / 'out')
    if attack == 'count': stack.pop(0)
    elif attack == 'extra-target': stack[-1] = ('extra.patch', *patch('old/3.cc', 'patched3\n', 'unexpected\n')[1:])
    elif attack == 'generated': stack[0] = ('generated.patch', *patch('out/Chromix/keep.o', 'old\n', 'compiled ARM object\n')[1:])
    elif attack == 'old-source': (src / 'old/0.cc').write_text('tampered\n')
    elif attack == 'symlink':
        target = src / 'handlers/storage.h'
        target.unlink()
        target.symlink_to(src / 'old/0.cc')
    with pytest.raises(ValueError):
        storage.private_migration(src, stack, tmp_path, manifest)
    assert storage.snapshot_outputs(src / 'out') == output


def test_output_hash_detects_same_size_same_mtime_mutation(tmp_path):
    put(tmp_path / 'keep.o', 'object-one')
    before = storage.snapshot_outputs(tmp_path)
    p = tmp_path / 'keep.o'
    ns = p.stat().st_mtime_ns
    p.write_text('object-two')
    os.utime(p, ns=(ns, ns))
    after = storage.snapshot_outputs(tmp_path)
    assert before != after
    assert storage.output_identity(before) != storage.output_identity(after)


def test_transaction_requires_both_blockers_and_preserves_mode(tmp_path):
    src, stack, manifest = fixture(tmp_path)
    before, after = storage.private_migration(src, stack, tmp_path, manifest)
    targets = manifest['migration']['sources']
    with pytest.raises(ValueError, match='blockers'):
        storage.publish_sources(src, before, after, targets)
    (src / storage.PENDING).write_text('pending')
    with pytest.raises(ValueError, match='blockers'):
        storage.publish_sources(src, before, after, targets)
    (src / '.chromix-layer-in-progress').write_text('pending')
    output = storage.snapshot_outputs(src / 'out')
    storage.publish_sources(src, before, after, targets)
    for name in targets:
        assert (src / name).read_bytes() == after[name]
        assert (src / name).stat().st_mode == before[name][1].st_mode
    assert (src / storage.PENDING).exists()
    assert storage.snapshot_outputs(src / 'out') == output


def test_only_two_existing_recipe_inputs_may_change():
    old = storage.load()['source_receipt']['recipe']
    storage.recipe_delta(old, source.recipe())
    bad = copy.deepcopy(source.recipe())
    bad['inputs']['tools/linux157_ua.py'] = '0' * 64
    with pytest.raises(ValueError, match='recipe delta'):
        storage.recipe_delta(old, bad)


def test_parent_selector_review_still_required():
    from tools import patch_selection
    bound = json.loads((ROOT / 'build/patch-selection-157.json').read_bytes())['platforms']['linux']['downstream']['recipe']
    if bound != source.recipe():
        with pytest.raises(patch_selection.SelectionError):
            patch_selection.select(ROOT, 'linux')


def test_fixed_stage6_chain_preserves_native_gate_and_failure_snapshots():
    text = workflow.render()
    assert text == (ROOT / workflow.OUT).read_text()
    jobs = yaml.safe_load(text)['jobs']
    assert set(jobs) == {'posix-6', 'posix-7', 'posix-8', 'verify-linux-arm64'}
    assert 'needs' not in jobs['posix-6']
    assert jobs['posix-7']['needs'] == 'posix-6'
    assert jobs['posix-8']['needs'] == 'posix-7'
    for stage in range(6, 9):
        steps = jobs[f'posix-{stage}']['steps']
        run = next(s for s in steps if s.get('id') == 'stage')
        assert f'--stage-index {stage}' in run['run']
        assert run['env']['CHROMIX_LINUX157_ARM64_STORAGE_STAGE5_RECOVERY'] == '1'
        assert 'CHROMIX_LINUX157_UA_RECOVERY' not in run['env']
        assert any(s.get('name') == 'Preserve latest failed Linux source/output checkpoint' for s in steps)
        assert any(s.get('name') == 'Verify handoff snapshot' for s in steps)
    steps = jobs['posix-6']['steps']
    assert not any(s.get('name') == 'Download tree from previous stage' for s in steps)
    restore = next(s for s in steps if s.get('name') == 'Restore exact ARM stage5 compiled Storage checkpoint')
    assert 'linux157_storage.py --phase restore --arch arm64' in restore['run']
    native = jobs['verify-linux-arm64']
    original = yaml.safe_load(base.NATIVE_LINUX_ARM64)['verify-linux-arm64']
    assert native['needs'] == ['posix-6', 'posix-7', 'posix-8']
    assert {k: v for k, v in native.items() if k != 'needs'} == {k: v for k, v in original.items() if k != 'needs'}
    assert native['runs-on'] == 'ubuntu-24.04-arm'


@pytest.mark.parametrize('stage,optin,other,allowed', [(3, '1', '0', False), (4, '1', '0', False), (5, '1', '0', False), (6, '0', '0', False), (6, '1', '1', False), (6, '1', '0', True), (7, '1', '0', True), (8, '1', '0', True), (9, '1', '0', False)])
def test_ci_storage_route_is_only_logical6_to8(tmp_path, stage, optin, other, allowed):
    work = tmp_path / 'work'
    put(work / 'src/out/Chromix/keep.o', 'latest compiled stage5')
    put(tmp_path / 'CHROMIUM_LINUX_VERSION', '157.0.8103.78\n')
    put(tmp_path / 'build/posix/ci-stage.sh', (ROOT / 'build/posix/ci-stage.sh').read_text())
    put(tmp_path / 'tools/linux157_source.py', "print('{}')\n")
    put(tmp_path / 'tools/linux157_build.py', "import os\nfrom pathlib import Path\nPath(os.environ['CALL_LOG']).write_text('continue')\nraise SystemExit(17)\n")
    put(tmp_path / 'bin/timeout', '#!/bin/sh\nshift 5\nexec "$@"\n')
    env = dict(os.environ, PATH=str(tmp_path / 'bin') + os.pathsep + os.environ['PATH'], CALL_LOG=str(tmp_path / 'calls'),
               CHROMIX_USE_UPSTREAM_CACHE='0', CHROMIX_BUILD_PROFILE='fast', CHROMIX_LINUX157_STORAGE_RECOVERY='1',
               CHROMIX_LINUX157_ARM64_STORAGE_STAGE5_RECOVERY=optin, CHROMIX_LINUX157_UA_RECOVERY=other,
               CHROMIX_LINUX157_LOCALE_RECOVERY='0', CHROMIX_LINUX157_PRE_GN_RECOVERY='0', CHROMIX_LINUX157_POST_GN_RECOVERY='0')
    result = subprocess.run(['bash', str(tmp_path / 'build/posix/ci-stage.sh'), '--platform', 'linux', '--arch', 'arm64',
                             '--workdir', str(work), '--stage-index', str(stage)], cwd=tmp_path, env=env, capture_output=True)
    assert result.returncode != 0 and (tmp_path / 'calls').exists() is allowed
    assert (work / 'src/out/Chromix/keep.o').read_text() == 'latest compiled stage5'


def test_actual_donor_file_and_output_hashes():
    location = os.environ.get('CHROMIX_STORAGE_DONOR')
    if not location:
        pytest.skip('requires digest-authenticated actual stage5 donor, not fixture migration')
    work = Path(location)
    m = storage.load()
    storage.prior.verify_files(work, m['files'])
    storage.prior.verify_files(work / 'src', m['source_receipt']['substituted_outputs'])
    storage.prior.verify_files(work / 'src', m['source_final']['outputs'])
    assert storage.output_identity(storage.snapshot_outputs(work / 'src/out')) == m['output_tree']


def test_fixed_caller_has_no_donor_override():
    data = yaml.safe_load((ROOT / '.github/workflows/build-linux157-arm64-storage-resume.yml').read_text())
    events = data.get('on', data.get(True))
    assert set(events) == {'workflow_dispatch'}
    assert set(events['workflow_dispatch']['inputs']) == {'compile_jobs'}
    assert data['concurrency']['cancel-in-progress'] is False
    assert data['jobs']['resume']['with']['linux157_storage_recovery'] is True
    assert data['jobs']['resume']['with']['arch'] == 'arm64'
    assert data['jobs']['resume']['with']['use_upstream_cache'] is False


@pytest.mark.parametrize('field,value', [('arch', 'x64'), ('runner', 'ubuntu-22.04'), ('linux157_storage_recovery', 'false'), ('use_upstream_cache', 'true'), ('resume_attempt', '2'), ('resume_run_id', '44639133090')])
def test_fixed_stage6_guard_rejects_input_drift(tmp_path, field, value):
    steps = yaml.safe_load(workflow.render())['jobs']['posix-6']['steps']
    guard = next(s for s in steps if s.get('name') == 'Require fixed ARM stage5-to-stage6 Storage continuation')
    values = {'github.repository': 'xiaozhou26/Chromix', 'github.event_name': 'workflow_dispatch', 'github.ref': 'refs/heads/main',
              'inputs.platform': 'linux', 'inputs.arch': 'arm64', 'inputs.runner': 'ubuntu-24.04', 'inputs.artifact': 'chromix-linux-arm64',
              'inputs.build_profile': 'fast', 'inputs.use_upstream_cache': 'false', 'inputs.linux157_storage_recovery': 'true',
              'inputs.linux157_pre_gn_recovery': 'false', 'inputs.linux157_post_gn_recovery': 'false', 'inputs.resume_migration': '',
              'inputs.resume_run_id': '', 'inputs.resume_artifact_ids': '', 'inputs.resume_attempt': '1', 'inputs.max-stages': '8'}
    values['inputs.' + field] = value
    text = guard['run']
    for key, val in values.items():
        text = text.replace('${{ ' + key + ' }}', val)
    assert '${{' not in text
    result = subprocess.run(['bash', '-c', text], cwd=tmp_path, capture_output=True)
    assert result.returncode != 0 and not list(tmp_path.iterdir())


def test_fixture_interrupted_multisource_publication_stays_blocked(tmp_path, monkeypatch):
    src, stack, manifest = fixture(tmp_path)
    before, after = storage.private_migration(src, stack, tmp_path, manifest)
    targets = manifest['migration']['sources']
    for name in (storage.PENDING, '.chromix-layer-in-progress'):
        (src / name).write_text('pending')
    output = storage.snapshot_outputs(src / 'out')
    original = storage.atomic_bytes
    calls = []
    def interrupt(path, raw, **kwargs):
        calls.append(path)
        if len(calls) == 2:
            raise OSError('publication interrupted')
        original(path, raw, **kwargs)
    monkeypatch.setattr(storage, 'atomic_bytes', interrupt)
    with pytest.raises(OSError, match='interrupted'):
        storage.publish_sources(src, before, after, targets)
    assert (src / storage.PENDING).exists() and (src / '.chromix-layer-in-progress').exists()
    assert not (src / storage.MARKER).exists()
    assert storage.snapshot_outputs(src / 'out') == output


def test_arm_route_never_falls_back_to_ua(tmp_path, monkeypatch):
    from tools import linux157_ua
    calls = []
    monkeypatch.setattr(storage, 'verify', lambda *args: calls.append('storage'))
    monkeypatch.setattr(linux157_ua, 'verify', lambda *args: calls.append('ua'))
    storage.verify_current(tmp_path, 'arm64')
    assert calls == ['storage']


@pytest.mark.parametrize('name', ['out/Chromix/gen/storage.h', 'build/config.gni', '../escape.cc', '/absolute.cc'])
def test_review_cannot_authorize_generated_output_or_gn(tmp_path, name):
    _, _, manifest = fixture(tmp_path)
    old = 'protocol/Storage.pdl'
    manifest['migration']['sources'][name] = manifest['migration']['sources'].pop(old)
    manifest['migration']['additions'][0]['targets'][0] = name
    with pytest.raises((ValueError, storage.arp.ApplyError)):
        storage.review(manifest)


def actual_selected_identity():
    manifest = storage.load()
    selected = copy.deepcopy(manifest['source_receipt']['chromix_selection'])
    extra = [{'path': p['patch'], 'sha256': p['sha256']} for p in manifest['migration']['additions']]
    selected['patches'] += extra
    selected['selection']['additions']['patches'] += extra
    selected['series_sha256'] = storage.canonical_series(ROOT, selected, (ROOT / 'patches/series').read_bytes())
    raw = [(p['path'], (ROOT / p['path']).read_bytes()) for p in selected['patches']]
    return manifest, selected, raw


def test_actual_old229_prefix_and_new232_canonical_series_are_distinct():
    manifest, selected, raw = actual_selected_identity()
    old = manifest['source_receipt']['chromix_selection']
    assert selected['series_sha256'] != old['series_sha256']
    assert selected['patches'][:229] == old['patches']
    assert storage.validate_additions(selected, raw, manifest) == manifest['migration']['additions']


@pytest.mark.parametrize('attack', ['old-prefix', 'old-series', 'new-series', 'addition-order', 'raw-bytes', 'multi-source', 'count'])
def test_storage232_contract_mutations_fail_closed(attack):
    manifest, selected, raw = actual_selected_identity()
    if attack == 'old-prefix': selected['patches'][10]['sha256'] = '0' * 64
    elif attack == 'old-series': manifest['source_receipt']['chromix_selection']['series_sha256'] = selected['series_sha256']
    elif attack == 'new-series': selected['series_sha256'] = manifest['source_receipt']['chromix_selection']['series_sha256']
    elif attack == 'addition-order': raw[-1], raw[-2] = raw[-2], raw[-1]
    elif attack == 'raw-bytes': raw[-1] = (raw[-1][0], raw[-1][1] + b'\n')
    elif attack == 'multi-source': manifest['migration']['additions'][0]['targets'].append(manifest['migration']['additions'][1]['targets'][0])
    elif attack == 'count': manifest['migration']['new_patch_count'] = 230
    with pytest.raises(ValueError):
        storage.validate_additions(selected, raw, manifest)


def test_actual_migrated232_checkpoint_with_strict_test_review():
    donor = os.environ.get('CHROMIX_STORAGE_MIGRATED')
    reviewed = os.environ.get('CHROMIX_STORAGE_REVIEW_REPO')
    if not donor or not reviewed:
        pytest.skip('requires actual latest donor transaction and isolated strict recipe review')
    repo = Path(reviewed)
    assert (repo / 'tools/linux157_storage.py').read_bytes() == (ROOT / 'tools/linux157_storage.py').read_bytes()
    assert (repo / storage.MANIFEST).read_bytes() == (ROOT / storage.MANIFEST).read_bytes()
    result = subprocess.run([os.sys.executable, str(repo / 'tools/linux157_storage.py'), '--phase', 'verify',
                             '--arch', 'arm64', '--workdir', donor, '--repo', str(repo)],
                            cwd=repo, capture_output=True, text=True, check=True)
    proof = json.loads(result.stdout)
    assert proof['old_patch_count'] == 229 and proof['new_patch_count'] == 232
    assert proof['runtime_verified'] is False
    result = subprocess.run([os.sys.executable, str(repo / 'tools/linux157_source.py'), '--phase', 'checkpoint',
                             '--arch', 'arm64', '--workdir', donor, '--repo', str(repo)],
                            cwd=repo, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)['source_verified'] is True
    assert storage.output_identity(storage.snapshot_outputs(Path(donor) / 'src/out')) == storage.load()['output_tree']
