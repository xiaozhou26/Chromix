"""Exact two-file TS migration: no unrelated replay, output cleanup or marker bypass."""
import copy
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migrate_linux156_ts_snapshot as migration
from tools.tests import test_migrate_restored_snapshot as fixtures

ROOT = Path(__file__).resolve().parents[2]
FIX = "d3e6b8868b688f6ecbc75d832acb71ba6701bb6d"


def blob(ref, name):
    return subprocess.check_output(["git", "-C", str(ROOT), "show", ref + ":" + name])


def load_module(path):
    spec = importlib.util.spec_from_file_location("linux156_reviewed_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def reviewed(tmp_path):
    previous, repo = tmp_path / 'previous', tmp_path / 'repo'
    subprocess.run(['git', 'clone', '--shared', '--no-checkout', str(ROOT), str(previous)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(previous), 'checkout', '--detach', migration.donor.HEAD], check=True, capture_output=True)
    shutil.copytree(previous, repo, ignore=shutil.ignore_patterns('.git'))
    for name in migration.TOOLS:
        (repo / name).write_bytes(blob(FIX, name))
    return previous, repo


def test_actual_fix_only_changes_two_reviewed_tools_and_preserves_226_key(reviewed):
    previous, repo = reviewed
    module = migration.authenticate_inputs(previous, repo)
    assert module.SOURCE156_REPAIRS['node_modules/typescript/lib/lib.dom.d.ts'][:2] == migration.SURFACE[migration.DOM]
    assert module.ROOT156_INTERMEDIATE_REPAIRS[migration.VALIDATOR][:2] == migration.SURFACE[migration.VALIDATOR]
    for arch in ('x64', 'arm64'):
        assert migration.shared.source_ready_key(previous, 'linux', arch) == migration.shared.source_ready_key(repo, 'linux', arch)
    assert len(migration.arp.patch_selection.select(repo, 'linux')[1]) == 226


@pytest.mark.parametrize('name', ['build/args.gn', 'build/build.sh', 'tools/install_linux_generators.py',
                                  'tools/linux_restored_generators.py', 'tools/prepare_restored_build.py',
                                  'tools/patch_selection.py', 'build/patch-selection-156.json'])
def test_actual_target_rejects_unrelated_or_unpinned_changes(reviewed, name):
    previous, repo = reviewed
    (repo / name).write_bytes((repo / name).read_bytes() + b'\nchanged')
    with pytest.raises(ValueError):
        migration.authenticate_inputs(previous, repo)


def generators_fixture(src, module):
    data = json.loads(gzip.decompress(blob(FIX, 'tools/tests/fixtures/linux156_generators.json.gz')))
    for name, text in data['sources'].items():
        fixtures.put(src, name, text)
    # Reconstruct the exact fully prepared donor, except its two known TS defects.
    for name, _, repaired, _ in module.source_plan(src, pins=module.PINS156)[1]:
        if name not in migration.SURFACE:
            (src / name).write_bytes(repaired)
    fixtures.put(src, migration.VALIDATOR, data['checkpoint']['helper'])
    return data


def test_real_repair_surface_has_exact_hashes_and_no_unrelated_replay(tmp_path):
    path = fixtures.put(tmp_path, 'reviewed.py', blob(FIX, 'tools/linux_restored_generators.py'))
    module = load_module(path)
    src = tmp_path / 'src'
    generators_fixture(src, module)
    identities, changes = module.source_plan(src, pins=module.PINS156)
    assert {change[0] for change in changes} == set(migration.SURFACE)
    for name, old, new, _ in changes:
        assert tuple(hashlib.sha256(raw).hexdigest() for raw in (old, new)) == migration.SURFACE[name]
    assert len(identities) == 16


@pytest.fixture(params=['x64', 'arm64'])
def small_snapshot(tmp_path, monkeypatch, request):
    monkeypatch.setattr(fixtures.Fixture, 'platform', 'linux')
    fx = fixtures.Fixture(tmp_path, arch=request.param, version='153.0.8010.36')
    patches = []
    for number in range(226):
        name = f'patch-target/file-{number}.cc'
        fixtures.put(fx.src, name, 'original\n')
        patches.append(fixtures.patch(name, 'original', 'patched'))
    for repo in (fx.previous, fx.repo):
        fixtures.series(repo, *patches)
    fx.prepare()
    module = load_module(fixtures.put(tmp_path, 'reviewed.py', blob(FIX, 'tools/linux_restored_generators.py')))
    generators_fixture(fx.src, module)
    fixtures.put(fx.src, 'out/Default/.ninja_log', '# ninja log v6\n0\t1\t100\tobj/cached.o\tabc\n')
    fixtures.put(fx.src, 'out/Default/gn', 'retained native GN')
    identities, _ = module.source_plan(fx.src, pins=module.PINS156)
    records = copy.deepcopy(identities)
    records.pop(migration.DOM)
    records[migration.VALIDATOR]['repaired_sha256'] = migration.SURFACE[migration.VALIDATOR][0]
    old_package = migration.package_digest(fx.src)
    changes = module.source_plan(fx.src, pins=module.PINS156)[1]
    new_package = migration.package_digest(fx.src, {name: new for name, _, new, _ in changes})
    monkeypatch.setattr(migration, 'PACKAGE_HASHES', (old_package, new_package))
    prepared = {'platform': 'linux', 'arch': fx.arch, 'ready_for_gn': True,
                'generator_fingerprint': {'typescript': {'package_sha256': old_package,
                                                        'esbuild': {'sources': records}}},
                'compiler': 'unchanged', 'environment': 'unchanged', 'tool_fingerprint': 'unchanged'}
    fixtures.put(fx.src, migration.PREPARED, json.dumps(prepared, sort_keys=True) + '\n')
    ninja = {'selected': {'version': '1.12.1'}, 'log': {'format': 6}, 'arch': fx.arch, 'host': ['linux', 'x64']}
    fixtures.put(fx.work, migration.restore_ninja.REPORT, json.dumps(ninja))
    pinned = dict(migration.donor.DONORS[fx.arch])
    for key, path in [('receipt_sha256', fx.src / migration.restore.MARKER),
                      ('args_sha256', fx.src / 'out/Default/args.gn'),
                      ('ninja_sha256', fx.work / migration.restore_ninja.REPORT)]:
        pinned[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    pinned['log_sha256'] = hashlib.sha256((fx.src / 'out/Default/.ninja_log').read_bytes()).hexdigest()
    pinned['log_size'] = (fx.src / 'out/Default/.ninja_log').stat().st_size
    monkeypatch.setitem(migration.donor.DONORS, fx.arch, pinned)
    monkeypatch.setattr(migration.donor, 'PATCH_RECEIPT_SHA256', hashlib.sha256((fx.src / migration.arp.MARKER).read_bytes()).hexdigest())
    monkeypatch.setitem(migration.PREPARED_HASHES, fx.arch, hashlib.sha256((fx.src / migration.PREPARED).read_bytes()).hexdigest())
    manifest = migration.donor.expected(fx.arch)
    origin = fixtures.put(tmp_path, 'origin.json', json.dumps(manifest))
    artifact = {**manifest['artifacts'][0], 'attempts': [{'status': 'success', 'size_in_bytes': pinned['size'],
                                                       'sha256': pinned['digest'].removeprefix('sha256:')}]}
    report = {**{k: manifest[k] for k in ('repository', 'head_sha', 'run_id')}, 'status': 'success',
              'publication': 'published', 'phase': 'complete', 'artifacts': [artifact],
              'total_size_in_bytes': 123,
              'volumes': [{'name': 'tree.tar.zst.001', 'artifact_id': pinned['artifact_id'],
                           'size_in_bytes': 123, 'sha256': 'a' * 64}]}
    download = fixtures.put(tmp_path, 'download.json', json.dumps(report))
    monkeypatch.setattr(migration, 'authenticate_inputs', lambda *args: module)
    fx.module, fx.origin, fx.download = module, origin, download
    fx.run_migration = lambda: migration.migrate(fx.work, fx.previous, fx.repo, fx.arch, origin, download)
    return fx


@pytest.mark.skipif(sys.platform != 'linux', reason='production renameat2 CAS requires Linux')
def test_real_atomic_two_file_migration_preserves_all_outputs_keys_receipts(small_snapshot):
    fx = small_snapshot
    before = fixtures.snapshot(fx.src)
    inventory = migration.output_inventory(fx.src / 'out/Default')
    report = fx.run_migration()
    assert report['status'] == 'migrated'
    assert report['old_patch_count'] == report['patch_count'] == 226
    assert report['previous_key'] == report['key']
    assert report['changed_files'] == sorted(migration.SURFACE)
    after = fixtures.snapshot(fx.src)
    for name, value in before.items():
        if name not in (*migration.SURFACE, migration.PREPARED):
            assert after[name] == value, name
    assert migration.output_inventory(fx.src / 'out/Default') == inventory
    assert not (fx.src / migration.shared.TRANSACTION).exists()
    assert json.loads((fx.src / migration.MARKER).read_bytes())['status'] == 'migrated'
    for name, (_, digest) in migration.SURFACE.items():
        assert hashlib.sha256((fx.src / name).read_bytes()).hexdigest() == digest
    assert migration.verify_source(fx.work, fx.repo)['patch_count'] == 226
    with pytest.raises(ValueError, match='already migrated'):
        fx.run_migration()


@pytest.mark.parametrize('name', [migration.shared.READY, migration.restore.MARKER, migration.arp.MARKER,
                                  'out/Default/args.gn', 'out/Default/.ninja_log', migration.DOM,
                                  migration.VALIDATOR, migration.PREPARED, 'patch-target/file-100.cc'])
def test_bad_identity_or_source_never_changes_tree(small_snapshot, name):
    fx = small_snapshot
    (fx.src / name).write_bytes((fx.src / name).read_bytes() + b'\nchanged')
    before = fixtures.snapshot(fx.src)
    with pytest.raises((ValueError, migration.arp.ApplyError)):
        fx.run_migration()
    assert fixtures.snapshot(fx.src) == before
    assert not (fx.src / migration.MARKER).exists()


@pytest.mark.parametrize('failure', ['origin', 'download', 'extra-surface', 'output-mutation', 'linked-source', 'tooling'])
def test_strict_boundary_rejects_mixed_inputs(small_snapshot, monkeypatch, failure):
    fx = small_snapshot
    if failure == 'origin':
        data = json.loads(fx.origin.read_bytes())
        data['run_id'] -= 1
        fx.origin.write_text(json.dumps(data))
    elif failure == 'download':
        data = json.loads(fx.download.read_bytes())
        data['artifacts'][0]['attempts'][-1]['sha256'] = 'b' * 64
        fx.download.write_text(json.dumps(data))
    elif failure == 'extra-surface':
        data = json.loads(gzip.decompress(blob(FIX, 'tools/tests/fixtures/linux156_generators.json.gz')))
        name = 'third_party/devtools-frontend/src/front_end/ui/legacy/ARIAUtils.ts'
        (fx.src / name).write_text(data['sources'][name])
    elif failure == 'linked-source':
        path = fx.src / migration.DOM
        external = fx.work / 'external-dom'
        path.rename(external)
        path.symlink_to(external)
    elif failure == 'tooling':
        (fx.core / 'domain_regex.list').write_bytes(b'changed\n')
    else:
        # The whole-output stat inventory detects even files not present in Ninja's log.
        out = fx.src / 'out/Default'
        original = migration.output_inventory(out)
        fixtures.put(out, 'obj/unlogged.o', 'latest output')
        assert migration.output_inventory(out) != original
        return
    before = fixtures.snapshot(fx.src)
    with pytest.raises((ValueError, migration.arp.ApplyError)):
        fx.run_migration()
    assert fixtures.snapshot(fx.src) == before
    assert not (fx.src / migration.MARKER).exists()


@pytest.mark.skipif(sys.platform != 'linux', reason='production renameat2 CAS requires Linux')
def test_final_source_verification_failure_retains_transaction(small_snapshot, monkeypatch):
    fx = small_snapshot
    original = migration.verify_patch_stack.verify
    calls = []
    def reject_final(*args, **kwargs):
        calls.append(args[0])
        if len(calls) == 2:
            raise ValueError('injected final reverse-forward failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(migration.verify_patch_stack, 'verify', reject_final)
    prepared = (fx.src / migration.PREPARED).read_bytes()
    with pytest.raises(ValueError, match='final reverse-forward'):
        fx.run_migration()
    assert len(calls) == 2
    assert (fx.src / migration.shared.TRANSACTION).exists()
    assert json.loads((fx.src / migration.MARKER).read_bytes())['status'] == 'in_progress'
    assert (fx.src / migration.PREPARED).read_bytes() == prepared


@pytest.mark.skipif(sys.platform != 'linux', reason='production renameat2 CAS requires Linux')
def test_interrupted_publish_keeps_blocker_and_never_claims_migrated(small_snapshot, monkeypatch):
    fx = small_snapshot
    original = fx.module._atomic_replace
    calls = []
    def interrupt(src, name, *args, **kwargs):
        calls.append(name)
        if len(calls) == 2:
            raise OSError('injected interruption')
        return original(src, name, *args, **kwargs)
    monkeypatch.setattr(fx.module, '_atomic_replace', interrupt)
    inventory = migration.output_inventory(fx.src / 'out/Default')
    marker = (fx.src / migration.PREPARED).read_bytes()
    with pytest.raises(OSError, match='injected'):
        fx.run_migration()
    assert len(calls) == 2
    assert (fx.src / migration.shared.TRANSACTION).exists()
    assert json.loads((fx.src / migration.MARKER).read_bytes())['status'] == 'in_progress'
    assert (fx.src / migration.PREPARED).read_bytes() == marker
    assert migration.output_inventory(fx.src / 'out/Default') == inventory
    with pytest.raises(ValueError, match='interrupted'):
        fx.run_migration()
