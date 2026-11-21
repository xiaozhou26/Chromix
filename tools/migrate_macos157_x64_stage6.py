#!/usr/bin/env python3
"""Migrate only the fixed mac157 Intel stage6 tree; preserve its original Ninja graph."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate_macos156_arm64_stage8_checkpoint as engine
import verify_patch_stack as verifier
import download_posix_snapshot as download

arp = engine.arp
shared = engine.migration
require = engine.require
ROOT = Path(__file__).resolve().parents[1]
PROFILE = 'build/macos/mac157-x64-stage6-checkpoint.json'
PROFILE_SHA256 = '41a439f0e9352a0dd3dd0df90046a35ab87d8905e707bb7a4cc298f149744b69'
RECEIPT = 'fingerprint-diagnostics/mac157-x64-stage6-resume.json'
ARCHIVED_SOURCE = 'fingerprint-diagnostics/mac157-x64-stage6-donor-source.json'
MARKER = '.chromix-macos157-x64-stage6-migration.json'
CONTINUATION = 'mac157-x64-stage6-continuation'
SURFACE = ('components/embedder_support/user_agent_utils.cc',)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load(repo=ROOT):
    raw = engine.read(repo, PROFILE)
    require(digest(raw) == PROFILE_SHA256, 'unreviewed fixed stage6 profile')
    return json.loads(raw)


def manifest(repo=ROOT):
    p = load(repo)
    return {key: p[key] for key in ('repository', 'platform', 'arch', 'workflow', 'run_id',
                                   'attempt', 'stage', 'head_sha', 'job_id', 'pattern', 'artifacts')}


def metadata(client, repo=ROOT):
    p = load(repo)
    run = client.get(f"/actions/runs/{p['run_id']}")
    job = client.get(f"/actions/jobs/{p['job_id']}")
    require(all(type(run.get(k)) is int for k in ('id', 'run_attempt'))
            and all(type(job.get(k)) is int for k in ('id', 'run_id', 'run_attempt')), 'integer producer identities required')
    require(all(run.get(k) == v for k, v in dict(id=p['run_id'], head_sha=p['head_sha'],
            run_attempt=1, head_branch='main', event='workflow_dispatch', status='completed',
            conclusion='failure', path='.github/workflows/build-macos-x64.yml').items()), 'wrong producer run')
    require(all(job.get(k) == v for k, v in dict(id=p['job_id'], run_id=p['run_id'], run_attempt=1,
            head_sha=p['head_sha'], status='completed', conclusion='success').items())
            and job.get('name', '').endswith('macos-x64 stage 6 (resume compile)'), 'wrong successful stage6 job')
    artifacts = client.items(f"/actions/runs/{p['run_id']}/artifacts", 'artifacts')
    selected = [a for a in artifacts if a.get('name', '').startswith(p['pattern'].removesuffix('*'))]
    require(len(selected) == 1 and all(selected[0].get(k) == v for k, v in p['artifacts'][0].items()),
            'exact unexpired stage6 single volume required')
    return manifest(repo)


def download_identity(origin, report, repo=ROOT):
    p, expected = load(repo), manifest(repo)
    require(json.loads(origin.read_bytes()) == expected, 'wrong stage6 origin')
    data = json.loads(report.read_bytes())
    require((data.get('status'), data.get('phase'), data.get('publication')) ==
            ('success', 'complete', 'published'), 'download was not verified and published')
    for key in ('repository', 'run_id', 'head_sha'):
        require(data.get(key) == expected[key], 'wrong download origin: ' + key)
    artifacts = data.get('artifacts', [])
    require(len(artifacts) == 1 and all(artifacts[0].get(k) == v for k, v in expected['artifacts'][0].items()),
            'wrong downloaded artifact')
    attempts = artifacts[0].get('attempts', [])
    require(attempts and attempts[-1].get('status') == 'success'
            and attempts[-1].get('size_in_bytes') == expected['artifacts'][0]['size_in_bytes']
            and 'sha256:' + attempts[-1].get('sha256', '') == expected['artifacts'][0]['digest'],
            'missing actual ZIP digest/size evidence')
    require(data.get('volumes') == [p['volume']]
            and data.get('total_size_in_bytes') == p['volume']['size_in_bytes'], 'wrong original compressed stream')
    return dict(manifest=expected, volume=p['volume'], producer_log=p['producer_log'])


@contextmanager
def historical_selection(previous, repo=ROOT):
    """Interpret authenticated old selection as data with the current patch engine."""
    p = load(repo)
    require(shared._donor_git(previous, repo, 'rev-parse', 'HEAD').decode().strip() == p['head_sha']
            and shared._donor_git(previous, repo, 'rev-parse', 'HEAD^{tree}').decode().strip() == p['tree']
            and not shared._donor_git(previous, repo, 'status', '--porcelain', '--untracked-files=all'),
            'clean exact producer checkout required')
    raw = [(entry['path'], engine.read(previous, entry['path'])) for entry in p['old_selection']['patches']]
    require(all(digest(data) == entry['sha256'] for (_, data), entry in zip(raw, p['old_selection']['patches'])),
            'historical selected patch bytes changed')
    original = arp.patch_selection.select
    def select(root, platform=None, **kwargs):
        if Path(root).resolve() == previous.resolve():
            require(platform == 'macos', 'historical adapter is macOS-only')
            return copy.deepcopy(p['old_selection']), raw
        return original(root, platform, **kwargs)
    arp.patch_selection.select = select
    try:
        yield
    finally:
        arp.patch_selection.select = original


def recipes(repo=ROOT, previous=None):
    p = load(repo)
    selected, raw = arp.patch_selection.select(repo, 'macos')
    require(selected == p['new_selection'] and len(raw) == 231, 'exact new231 selection required')
    require(selected['patches'][:-1] == p['old_selection']['patches'], 'old230 patches changed')
    require(shared.source_ready_key(repo, 'macos', 'x64') == p['new_key'], 'wrong new preparation key')
    for name, values in p['recipes'].items():
        require(digest(engine.read(repo, name)) == values['new'], 'unreviewed current recipe: ' + name)
        if previous is not None:
            require(digest(engine.read(previous, name)) == values['old'], 'unreviewed producer recipe: ' + name)
    target_only = p['target_recipe_audit']['target_only_inputs']
    for name, expected in target_only.items():
        require(digest(engine.read(repo, name)) == expected, 'unreviewed target-only tooling: ' + name)
    if previous is not None:
        with historical_selection(previous, repo):
            require(shared.source_ready_key(previous, 'macos', 'x64') == p['old_key'], 'wrong old preparation key')
    return {**{name: values['new'] for name, values in p['recipes'].items()}, **target_only}


def canonical(report, original):
    result = copy.deepcopy(report)
    for field in ('regex', 'list'):
        result['identity'][field]['path'] = original['identity'][field]['path']
    return result


def stream_inventory(root):
    """Hash actual bytes, modes, mtimes and links; never infer outputs from filenames."""
    entries = {}
    for name, info in sorted(engine.output_inventory(root).items()):
        mode, size, mtime, _, _, link = info
        if stat.S_ISDIR(mode):
            continue
        require(stat.S_ISREG(mode) or stat.S_ISLNK(mode), 'unsupported output type: ' + name)
        entries[name] = dict(mode=mode, size=size, mtime_ns=mtime, symlink=link,
                             sha256=file_hash(root / name) if stat.S_ISREG(mode) else None)
    return dict(entries=len(entries), bytes=sum(x['size'] for x in entries.values()),
                sha256=digest(arp._json(entries)))


def build_state(work):
    src, out = work / 'src', work / 'src/out/Default'
    graphs = {p.relative_to(out).as_posix(): digest(engine.read(out, p.relative_to(out).as_posix()))
              for p in sorted(out.rglob('*.ninja'))}
    require(graphs and 'build.ninja' in graphs, 'original Ninja graph missing')
    protected = dict(graphs)
    for name in ('args.gn', 'build.ninja.d', 'build.ninja.stamp'):
        protected[name] = digest(engine.read(out, name))
    require(engine.read(out, '.ninja_log').startswith(b'# ninja log v7\n'), 'wrong Ninja log version')
    require(engine.read(out, '.ninja_deps').startswith(b'# ninjadeps\n\x04\x00\x00\x00'), 'wrong Ninja deps version')
    pgo = {p.relative_to(src).as_posix(): file_hash(p) for p in sorted(src.rglob('*.profdata'))}
    return dict(graph=digest(arp._json(protected)), pgo=pgo,
                ninja_log=digest(engine.read(out, '.ninja_log')), ninja_deps=digest(engine.read(out, '.ninja_deps')))


def source_plan(work, previous, repo=ROOT):
    p, src = load(repo), work / 'src'
    core, tooling = work / 'tooling/ungoogled-chromium', work / 'tooling/ungoogled-chromium-macos'
    with historical_selection(previous, repo):
        old_identity, old_stack, old_lite = arp._load(previous, core, tooling, 'macos', src=src)
        shared._previous_completed(src, old_identity, shared._names(old_stack, old_lite))
        original = canonical(verifier.verify(src, previous, core=core, tooling=tooling, platform='macos'), p['source_report'])
    require(original == p['source_report'] and original['patch_count'] == 230, 'actual old230 source proof changed')
    identity, stack, lite = arp._load(repo, core, tooling, 'macos', src=src)
    require(old_lite == lite and identity['lite'] == old_identity['lite'], 'lite payload changed')
    require(len(stack) == 231 and stack[:-1] == old_stack, 'transformed old230 stack changed')
    require([x[2][0][:2] for x in stack[-1:]] == [(name, 'modify') for name in SURFACE]
            and all(len(x[2]) == 1 for x in stack[-1:]), 'exact one-file surface required')
    names = shared._names(stack, lite) | {shared.SOURCE_VERSION}
    before = arp._snapshot(src, names)
    for name in SURFACE:
        require(before[name][1] is not None and before[name][1].st_nlink == 1, 'unlinked source required: ' + name)
    temporary_root = Path(tempfile.gettempdir()).resolve()
    require(not any(temporary_root.is_relative_to(path.resolve()) for path in (work, previous, repo)), 'private scratch required')
    with tempfile.TemporaryDirectory(prefix='mac157-stage6-source-', dir=temporary_root) as temporary:
        root, stage = Path(temporary), Path(temporary) / 'src'
        stage.mkdir()
        for name, (raw, info) in before.items():
            if raw is not None:
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                target.chmod(stat.S_IMODE(info.st_mode))
        program = arp.select_patch_program(['gpatch', 'patch'], (work, previous, repo))
        shared._reverse(stage, root, old_stack, program)
        arp.run_apply(stage, repo, core, tooling, 'macos', program)
        arp.run_apply(stage, repo, core, tooling, 'macos', program, check=True)
        (stage / '.chromix-domain-substituted').write_bytes(engine.read(src, '.chromix-domain-substituted'))
        verified = canonical(verifier.verify(stage, repo, core=core, tooling=tooling, platform='macos'), original)
        after = arp._snapshot(stage, names)
        require({n for n in names if before[n][0] != after[n][0]} == set(SURFACE), 'unrelated source changed')
        require(verified['patch_count'] == 231, 'incomplete new231 source proof')
        source_hashes = {n: dict(before=digest(before[n][0]), after=digest(after[n][0])) for n in SURFACE}
        require(source_hashes == p['source_hashes'], 'unreviewed full source pre/postimage')
        marker = json.loads(engine.read(stage, arp.MARKER))
        marker['identity'] = verified['identity']
        marker['identity_sha256'] = digest(arp._json(verified['identity']))
        require(digest(arp._json(verified)) == p['new_source_report_sha256'], 'unreviewed complete new source report')
    shared._unchanged(src, before)
    return before, {name: after[name][0] for name in SURFACE}, marker, verified


def identity(repo=ROOT):
    p = load(repo)
    return dict(status='verified', operation='mac157-x64-stage6-230-to-231',
                profile_sha256=PROFILE_SHA256, manifest=manifest(repo), old_patch_count=230, patch_count=231,
                previous_key=p['old_key'], preparation_key=p['new_key'], changed_files=sorted(SURFACE))


def protected_files(work, repo=ROOT, migrated=False):
    p = load(repo)
    for name, expected in p['pinned_files'].items():
        if migrated and name in ('src/.chromix-restored-patches.json', 'fingerprint-diagnostics/source-final.json'):
            continue
        require(digest(engine.read(work, name)) == expected, 'protected donor file changed: ' + name)
    require(engine.read(work, 'src/.chromix-restored-build-prepared.json') == engine.read(work, 'upstream-cache-preparation.json'),
            'preparation receipt mismatch')
    prepared = json.loads(engine.read(work, 'upstream-cache-preparation.json'))
    require(prepared['ready_for_gn'] is True and prepared['removed_gn'] is False
            and prepared['compiled_outputs']['removed_outputs'] == 0
            and all(prepared['counters'][name] == 0 for name in
                    ('first_finish', 'tool_swap_invalidations', 'toolchain_invalidated_outputs')), 'destructive preparation forbidden')
    require(arp.patch_selection.source_version(work / 'src') == p['version'], 'source version changed')
    shared.restore.verify_restored(work, 'macos', 'x64', repo=repo)
    shared._verify_tooling(work, repo, (work, repo), 'macos')


def migrate(work, previous, origin, report, repo=ROOT):
    require(re.fullmatch(r'[1-9][0-9]*', os.environ.get('GITHUB_RUN_ID', '')), 'continuation run required')
    provenance = download_identity(origin, report, repo)
    recipe = recipes(repo, previous)
    protected_files(work, repo)
    src, out = work / 'src', work / 'src/out/Default'
    shared._ready(src, load(repo)['old_key'])
    for name in (MARKER, shared.TRANSACTION, arp.IN_PROGRESS):
        require(not os.path.lexists(src / name), 'migration already present or interrupted')
    for name in (RECEIPT, ARCHIVED_SOURCE):
        require(not os.path.lexists(work / name), 'migration evidence already present')
    inventory = engine.output_inventory(src)
    state, out_before = build_state(work), stream_inventory(out)
    require(state == load(repo)['build_state'] and out_before == load(repo)['out_stream'], 'exact archived build state required')
    before, changes, marker, verified = source_plan(work, previous, repo)
    require(engine.output_inventory(src) == inventory, 'planning changed original source or outputs')
    markers = {name: (shared._marker(src, name).read_bytes(), shared._marker(src, name).stat()) for name in
               (shared.READY, arp.MARKER, shared.restore.MARKER, '.chromix-restored-build-prepared.json',
                '.chromix-toolchain-ready', '.chromix-domain-substituted')}
    old_source = engine.read(work, 'fingerprint-diagnostics/source-final.json')
    result = dict(identity(repo), provenance=provenance, recipes=recipe, state=state,
                  continuation_run=os.environ['GITHUB_RUN_ID'], source_hashes=load(repo)['source_hashes'],
                  out_stream_before=out_before, source_before_sha256=digest(old_source),
                  source_after_sha256=digest(arp._json(verified)), patch_receipt_sha256=digest(arp._json(marker)))
    shared._claim(src / shared.TRANSACTION, arp._json(dict(result, status='in_progress')))
    shared._claim(src / arp.IN_PROGRESS, arp._json(dict(result, status='in_progress')))
    shared._unchanged(src, before | markers)
    require(recipes(repo, previous) == recipe, 'recipes raced during migration')
    for name, raw in changes.items():
        info = before[name][1]
        arp._atomic_write(src / name, raw, stat.S_IMODE(info.st_mode), max(time.time_ns(), info.st_mtime_ns + 1_000_000_000))
    shared._unchanged(src, {n: value for n, value in (before | markers).items() if n not in SURFACE})
    difference = engine.inventory_difference(inventory, engine.output_inventory(src))
    allowed = set(SURFACE) | {shared.TRANSACTION, arp.IN_PROGRESS}
    for name in difference:
        if name not in allowed:
            require(stat.S_ISDIR(inventory[name][0]) and any(Path(name) in Path(n).parents for n in SURFACE),
                    'unrelated source/output changed: ' + name)
    out_after = stream_inventory(out)
    require(out_before == out_after and build_state(work) == state, 'GN/out/PGO/Ninja state changed')
    result['out_stream_after'] = out_after
    result['source_unchanged_outputs'] = True
    require(engine.read(work, 'fingerprint-diagnostics/source-final.json') == old_source, 'source receipt raced')
    arp._atomic_write(work / ARCHIVED_SOURCE, old_source)
    arp._atomic_write(src / arp.MARKER, arp._json(marker))
    arp._atomic_write(src / shared.READY, (load(repo)['new_key'] + '\n').encode())
    arp._atomic_write(work / 'fingerprint-diagnostics/source-final.json', arp._json(verified))
    arp._atomic_write(src / MARKER, arp._json(result))
    arp._atomic_write(work / RECEIPT, arp._json(result))
    (src / arp.IN_PROGRESS).unlink()
    current, stack, lite = arp._load(repo, work / 'tooling/ungoogled-chromium', work / 'tooling/ungoogled-chromium-macos', 'macos')
    shared._previous_completed(src, current, shared._names(stack, lite))
    (src / shared.TRANSACTION).unlink()
    return result


def source_state(work, repo=ROOT):
    p, src = load(repo), work / 'src'
    receipt = json.loads(engine.read(work, RECEIPT))
    require(all(receipt.get(k) == v for k, v in identity(repo).items())
            and receipt.get('continuation_run') == os.environ.get('GITHUB_RUN_ID'), 'unadmitted run/profile')
    require(engine.read(src, MARKER) == engine.read(work, RECEIPT), 'migration receipts differ')
    require(recipes(repo) == receipt['recipes'], 'admitted recipes changed')
    protected_files(work, repo, migrated=True)
    shared._ready(src, p['new_key'])
    for name in (shared.TRANSACTION, arp.IN_PROGRESS):
        require(not os.path.lexists(src / name), 'interrupted migration')
    require(digest(engine.read(work, ARCHIVED_SOURCE)) == receipt['source_before_sha256'], 'old source proof changed')
    require(digest(engine.read(src, arp.MARKER)) == receipt['patch_receipt_sha256'], 'new patch receipt changed')
    original = json.loads(engine.read(work, 'fingerprint-diagnostics/source-final.json'))
    verified = canonical(verifier.verify(src, repo, core=work / 'tooling/ungoogled-chromium',
                         tooling=work / 'tooling/ungoogled-chromium-macos', platform='macos'), original)
    require(verified == original and digest(arp._json(verified)) == p['new_source_report_sha256'], 'actual new231 source changed')
    state = build_state(work)
    require(state['graph'] == receipt['state']['graph'] and state['pgo'] == receipt['state']['pgo'], 'GN/PGO changed')
    return state


def native_state(work, repo=ROOT):
    src = work / 'src'
    old = json.loads(engine.read(work, 'upstream-cache-preparation.json'))
    require(engine.prepared.host_identity() == ('macos', 'x64'), 'native macOS Intel required')
    require(engine.prepared.environments_compatible(old['environment'], engine.prepared.environment_identity(src, 'macos')),
            'SDK/toolchain environment changed; no prepare fallback')
    require(engine.prepared.inspect_native_tools(src, 'macos', 'x64')['native_tools'], 'native tool identity failed')
    require(engine.prepared.tool_fingerprint(src, 'macos', 'x64') == old['tool_fingerprint'], 'toolchain bytes changed')
    generators = engine.prepared.generator_fingerprint(src, 'macos', 'x64')
    generators['macos154'] = engine.macos_restored_generators.prepare(src, host_arch='x64', repo=repo)
    require(generators == old['generator_fingerprint'], 'generator bytes changed')
    engine.macos_runtime.verify_runtime_loader(src, 'x64')


def continuation(work, stage, phase, repo=ROOT):
    require(1 <= stage <= 8 and phase in ('before', 'after'), 'invalid stage/phase')
    require(os.environ.get('CHROMIX_BUILD_PROFILE') == 'fast' and os.environ.get('CHROMIX_USE_UPSTREAM_CACHE') == '1',
            'fixed fast profile and required cache only')
    require(re.fullmatch(r'[1-9][0-9]*', os.environ.get('GITHUB_RUN_ATTEMPT', '')), 'attempt required')
    state = source_state(work, repo)
    receipt = json.loads(engine.read(work, RECEIPT))
    if phase == 'before':
        if stage == 1:
            require(state == receipt['state'], 'initial Ninja state changed')
        else:
            engine.previous_handoff(work, CONTINUATION, stage, 6, identity(repo), state)
    result = dict(identity(repo), continuation_run=os.environ['GITHUB_RUN_ID'],
                  continuation_attempt=os.environ['GITHUB_RUN_ATTEMPT'], continuation_stage=stage,
                  logical_stage=6 + stage, phase=phase, state=state)
    output = work / 'fingerprint-diagnostics' / f'{CONTINUATION}-s{stage}-{phase}-attempt-{os.environ["GITHUB_RUN_ATTEMPT"]}.json'
    require(not os.path.lexists(output), 'continuation evidence already exists')
    output.write_bytes(arp._json(result))
    return result


def verify_checkpoint(work, stage, repo=ROOT):
    """Recheck current source/state against this attempt's completed stage gate."""
    require(1 <= stage <= 8, 'invalid checkpoint stage')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '')
    require(re.fullmatch(r'[1-9][0-9]*', attempt), 'checkpoint attempt required')
    state = source_state(work, repo)
    expected = dict(identity(repo), continuation_run=os.environ.get('GITHUB_RUN_ID'),
                    continuation_attempt=attempt, continuation_stage=stage,
                    logical_stage=6 + stage, phase='after', state=state)
    name = f'fingerprint-diagnostics/{CONTINUATION}-s{stage}-after-attempt-{attempt}.json'
    require(json.loads(engine.read(work, name)) == expected, 'same-attempt completed source gate required')
    return dict(expected, qualification='Verified resumable tree; not compiled or runtime verified.')


def compile_tree(work, stage, repo=ROOT):
    state = source_state(work, repo)
    name = f'fingerprint-diagnostics/{CONTINUATION}-s{stage}-before-attempt-{os.environ.get("GITHUB_RUN_ATTEMPT")}.json'
    expected = dict(identity(repo), continuation_run=os.environ.get('GITHUB_RUN_ID'),
                    continuation_attempt=os.environ.get('GITHUB_RUN_ATTEMPT'), continuation_stage=stage,
                    logical_stage=6 + stage, phase='before', state=state)
    require(json.loads(engine.read(work, name)) == expected, 'same-attempt source gate required')
    native_state(work, repo)
    ninja = Path(shutil.which('ninja') or 'missing').resolve()
    require(engine.restore_ninja.host_identity() == ('macos', 'x64')
            and engine.restore_ninja.binary_architectures(ninja, 'macos') == {'x64'}
            and engine.restore_ninja.probe_version(ninja, work) == '1.13.2', 'native Ninja 1.13.2 required')
    jobs = os.environ.get('CHROMIX_JOBS', '')
    require(re.fullmatch(r'[1-9][0-9]*', jobs), 'positive compile job count required')
    return engine.frozen_ninja(ninja, work, jobs, {k: v for k, v in os.environ.items() if not k.startswith('DYLD_')})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('metadata', 'admit', 'check', 'verify', 'compile'), required=True)
    for name in ('repo', 'workdir', 'previous-repo', 'origin', 'download-report', 'report'):
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--stage', type=int)
    parser.add_argument('--phase', choices=('before', 'after'))
    args = parser.parse_args(argv)
    repo = (args.repo or ROOT).resolve()
    try:
        if args.mode == 'metadata':
            require(args.report is not None, 'metadata report required')
            result = metadata(engine.snapshot.Client(load(repo)['repository'], os.environ.get('GH_TOKEN', '')), repo)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_bytes(arp._json(result))
            if os.environ.get('GITHUB_OUTPUT'):
                with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                    output.write(f"head_sha={result['head_sha']}\npattern={result['pattern']}\n")
        else:
            require(args.workdir is not None, 'workdir required')
            work = args.workdir.resolve()
            if args.mode == 'admit':
                require(all((args.previous_repo, args.origin, args.download_report)), 'provenance required')
                result = migrate(work, args.previous_repo.resolve(), args.origin, args.download_report, repo)
            elif args.mode == 'check':
                require(args.stage is not None and args.phase is not None, 'stage/phase required')
                result = continuation(work, args.stage, args.phase, repo)
            elif args.mode == 'verify':
                require(args.stage is not None, 'checkpoint stage required')
                result = verify_checkpoint(work, args.stage, repo)
            else:
                require(args.stage is not None and 1 <= args.stage <= 8, 'compile stage required')
                return compile_tree(work, args.stage, repo)
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f'mac157 stage6 recovery rejected: {error}; restore the exact donor again', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
