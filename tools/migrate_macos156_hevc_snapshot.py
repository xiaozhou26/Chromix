#!/usr/bin/env python3
"""Exact ARM stage3 226-to-228 source migration; no preparation or GN regeneration."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from . import validate_macos156_arm64_stage3_checkpoint as donor
    from . import verify_patch_stack as verifier
except ImportError:
    import validate_macos156_arm64_stage3_checkpoint as donor
    import verify_patch_stack as verifier

arm = donor.arm
arp = arm.arp
shared = arm.migration
require = arm.require
sha = arm.sha
MARKER = '.chromix-macos156-hevc-migration.json'
RECEIPT = 'fingerprint-diagnostics/mac156-arm64-hevc-resume.json'
CONTINUATION = 'mac156-arm64-hevc-continuation'
KEY = ('macos|arm64|156.0.8078.79|28a211ab4ab6b83d919966a4f58a6df23cf4fb29|'
       '31f01756a43d55b2c4211e73ca329bfab4ebfcb2|'
       'f635269fac91c3d14c4bb3bca0680391d83fc4a26fa76f91c990f610f0c16037')
SOURCE_IDENTITY = 'e1a874de46560c0f9f3051bd8c7c065f1c8400a32fd559474ad84cd3c8d573b8'
SOURCE_OUTPUTS = '4f6e31985fcb33b36543d578c3a5a624cd1d358c32e1845c19e946d6b418b26c'
SELECTOR = ('d5a58a1849a2ac076ac1e04631c379423a80a9214174c543db729a2362f10b9f',
            'b50202281b53e1a6d1f2e200d26b84434b2e0f3b293958f3bf144460d94024d6')
MANIFEST = ('92f552bcfeb8eedc06e2173f2c02ed7e9afb65ad630bf11320a7e8a5191e79da',
            '829004d64306565f22e63ad670befd802987b6572c17f9d7e470a76f974f86f8')
PATCHES = {
    'patches/chromium156-additions/0234-macos-empty-angle-native-default.patch':
        '4180de95a9097d230faaa29006cd959437279bdd8251a6eebf6b07e89ff7e944',
    'patches/chromium156-additions/0235-macos-hevc-recorder-mime.patch':
        'b9f895112210e0753abe0ff4c8a065db0b26f006ba763819b2530969584eb961',
}
SURFACE = {
    'ui/gl/init/gl_display_initializer.cc': (
        '75e3955bf3090715e67b363178dfd46c28b93c86e97687c68e6bbc940483f2fe',
        '920ce01bf28b390a1a68c0b2f4424da0821744b0aa555845b987fc772810cf43'),
    'third_party/blink/renderer/modules/mediarecorder/media_recorder_handler.cc': (
        '35f467590d8fc68482e71522690d7bd806a5c067293f86025b834c3ed7e12eb5',
        '7907b48f0d6167f2ee5666fa04f0e54981746728c00aa3e76d846419fb2fd40c'),
}
ARCHIVED_SOURCE = 'fingerprint-diagnostics/mac156-arm64-hevc-donor-source.json'


def canonical(report, original):
    result = copy.deepcopy(report)
    for field in ('regex', 'list'):
        result['identity'][field]['path'] = original['identity'][field]['path']
    return result


def recipes(repo, previous=None):
    require(shared.source_ready_key(repo, 'macos', 'arm64') == KEY, 'exact 228 preparation key required')
    result = {name: sha(arm.recipe_bytes(name, arm.read(repo, name))) for name in
              dict.fromkeys((*arm.RECIPE_FILES, *donor.TOOL_BASELINES, arp.patch_selection.MANIFEST156))}
    require(sha(arm.read(repo, 'tools/patch_selection.py')) == SELECTOR[1], 'unreviewed selector')
    require(sha(arm.read(repo, arp.patch_selection.MANIFEST156)) == MANIFEST[1], 'unreviewed manifest')
    _, patches = arp.patch_selection.select(repo, 'macos')
    require(len(patches) == 228 and [(n, sha(b)) for n, b in patches[-2:]] == list(PATCHES.items()),
            'exact macOS-only 226-to-228 additions required')
    for name, digest in donor.TOOL_BASELINES.items():
        require(result[name] == digest, 'native acceptance/probe tooling must not change: ' + name)
    for name, digest in PATCHES.items():
        require(sha(arm.read(repo, name)) == digest, 'reviewed source patch changed: ' + name)
    if previous is not None:
        require(shared._donor_git(previous, repo, 'rev-parse', 'HEAD').decode().strip() == donor.SHA
                and shared._donor_git(previous, repo, 'rev-parse', 'HEAD^{tree}').decode().strip() == donor.TREE
                and not shared._donor_git(previous, repo, 'status', '--porcelain', '--untracked-files=all'),
                'clean exact stage3 previous checkout required')
        _, old = arp.patch_selection.select(previous, 'macos')
        require(len(old) == 226 and patches[:-2] == old, 'original 226 selected patches changed')
        for name, pair in (('tools/patch_selection.py', SELECTOR), (arp.patch_selection.MANIFEST156, MANIFEST)):
            require(sha(arm.read(previous, name)) == pair[0], 'previous recipe preimage changed')
        original = arm.recipes(previous)
        for name, digest in original.items():
            if name != 'tools/patch_selection.py':
                require(result[name] == digest, 'unrelated recipe changed: ' + name)
        for platform in ('linux', 'windows'):
            require(arp.patch_selection.select(previous, platform)[1] == arp.patch_selection.select(repo, platform)[1],
                    'non-macOS selected patches changed')
    return result


def source_plan(work, previous, repo):
    """Reverse all 226 patches, apply all 228, and verify complete files in private scratch."""
    src = work / 'src'
    core, tooling = work / 'tooling/ungoogled-chromium', work / 'tooling/ungoogled-chromium-macos'
    old_identity, old_patches, old_lite = arp._load(previous, core, tooling, 'macos')
    identity, patches, lite = arp._load(repo, core, tooling, 'macos')
    require(old_lite == lite and old_identity['lite'] == identity['lite'], 'lite payload changed')
    shared._previous_completed(src, old_identity, shared._names(old_patches, old_lite))
    original = json.loads(arm.read(work, 'fingerprint-diagnostics/source-final.json'))
    old_report = canonical(verifier.verify(src, previous, core=core, tooling=tooling, platform='macos'), original)
    require(old_report == original and old_report['patch_count'] == 226
            and arm.identity_hash(old_report['identity']) == arm.SOURCE_IDENTITY
            and arm.identity_hash(old_report['outputs']) == arm.SOURCE_OUTPUTS, 'old full source verification changed')
    names = shared._names(patches, lite) | shared._names(old_patches, old_lite) | {shared.SOURCE_VERSION}
    before = arp._snapshot(src, names)
    for name, (pre, post) in SURFACE.items():
        require(before[name][1] is not None and before[name][1].st_nlink == 1
                and sha(before[name][0]) == pre, 'exact unlinked source preimage required: ' + name)
    temp_root = Path(tempfile.gettempdir()).resolve()
    require(not any(temp_root.is_relative_to(p.resolve()) for p in (work, previous, repo)), 'scratch must be private')
    program = arp.select_patch_program(['gpatch', 'patch'], (work, previous, repo))
    with tempfile.TemporaryDirectory(prefix='mac156-hevc-plan-', dir=temp_root) as temporary:
        scratch = Path(temporary)
        stage = scratch / 'src'
        stage.mkdir()
        for name, (raw, info) in before.items():
            if raw is not None:
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                target.chmod(stat.S_IMODE(info.st_mode))
        shared._reverse(stage, scratch, old_patches, program)
        arp.run_apply(stage, repo, core, tooling, 'macos', program)
        arp.run_apply(stage, repo, core, tooling, 'macos', program, check=True)
        (stage / '.chromix-domain-substituted').write_bytes(arm.read(src, '.chromix-domain-substituted'))
        verified = canonical(verifier.verify(stage, repo, core=core, tooling=tooling, platform='macos'), original)
        require(verified['patch_count'] == 228 and arm.identity_hash(verified['identity']) == SOURCE_IDENTITY
                and arm.identity_hash(verified['outputs']) == SOURCE_OUTPUTS, 'new full source verification changed')
        after = arp._snapshot(stage, names)
        changed = {name for name in names if before[name][0] != after[name][0]}
        require(changed == set(SURFACE), 'migration changes more than the two reviewed source files')
        for name, (_, post) in SURFACE.items():
            require(sha(after[name][0]) == post, 'exact source postimage required: ' + name)
        patch_receipt = json.loads(arm.read(stage, arp.MARKER))
        patch_receipt['identity'] = verified['identity']
        patch_receipt['identity_sha256'] = arm.identity_hash(verified['identity'])
    shared._unchanged(src, before)
    return before, {name: after[name][0] for name in SURFACE}, patch_receipt, verified


def identity():
    return dict(status='verified', operation='mac156-arm64-stage3-hevc-source-migration',
                repository=arm.REPOSITORY, run_id=donor.RUN, job_id=donor.JOB, attempt=1, stage=3,
                head_sha=donor.SHA, tree=donor.TREE, artifacts=donor.ARTIFACTS,
                platform='macos', arch='arm64', version=arm.VERSION, previous_key=arm.KEY,
                preparation_key=KEY, old_patch_count=226, patch_count=228,
                source_identity=SOURCE_IDENTITY, source_outputs=SOURCE_OUTPUTS,
                changed_files=sorted(SURFACE), completed_logical_stage=12)


def migrate(work, previous, repo, origin, download_report):
    require(re.fullmatch(r'[1-9][0-9]*', os.environ.get('GITHUB_RUN_ID', '')), 'continuation run required')
    provenance = donor.download_identity(origin, download_report)
    recipe = recipes(repo, previous)
    old, handoff = donor.donor_identity(work)
    require(arm.recipes(previous) == old['recipes'], 'historical admission recipes changed')
    state = arm.source_state(previous, work)
    require(state == handoff['state'], 'exact compiled stage3 state required')
    src, out = work / 'src', work / 'src/out/Default'
    inventory = arm.output_inventory(out)
    arm.native_state(repo, work)
    donor.compiled_browser(repo, work)
    shared.restore.verify_restored(work, 'macos', 'arm64', repo=repo)
    shared._verify_tooling(work, repo, (work, previous, repo), 'macos')
    for path in (src / MARKER, work / RECEIPT, work / ARCHIVED_SOURCE):
        require(not path.exists() and not path.is_symlink(), 'migration already present')
    require(arm.output_inventory(out) == inventory, 'native admission changed donor outputs')
    before, changes, patch_receipt, verified = source_plan(work, previous, repo)
    require(arm.output_inventory(out) == inventory and arm.source_state(previous, work) == state,
            'planning modified donor output/state')
    markers = {name: (shared._marker(src, name).read_bytes(), shared._marker(src, name).stat())
               for name in (shared.READY, arp.MARKER, shared.restore.MARKER,
                            '.chromix-restored-build-prepared.json', '.chromix-toolchain-ready',
                            '.chromix-domain-substituted')}
    original_source = arm.read(work, 'fingerprint-diagnostics/source-final.json')
    result = dict(identity(), provenance=provenance, recipes=recipe, state=state,
                  continuation_run=os.environ['GITHUB_RUN_ID'], donor_receipts=donor.DONOR_RECEIPTS,
                  patch_receipt_sha256=sha(arp._json(patch_receipt)),
                  source_report_sha256=sha(arp._json(verified)))
    shared._claim(src / shared.TRANSACTION, arp._json(dict(result, status='in_progress')))
    shared._claim(src / arp.IN_PROGRESS, arp._json(dict(result, status='in_progress')))
    shared._unchanged(src, before | markers)
    require(recipes(repo, previous) == recipe, 'recipes changed during planning')
    for name, raw in changes.items():
        info = before[name][1]
        arp._atomic_write(src / name, raw, stat.S_IMODE(info.st_mode),
                          max(time.time_ns(), info.st_mtime_ns + 1_000_000_000))
    shared._unchanged(src, {name: value for name, value in (before | markers).items() if name not in SURFACE})
    require(arm.output_inventory(out) == inventory, 'source migration modified GN/out/objects/PGO')
    require(arm.read(work, 'fingerprint-diagnostics/source-final.json') == original_source, 'source receipt raced')
    arp._atomic_write(work / ARCHIVED_SOURCE, original_source)
    arp._atomic_write(src / arp.MARKER, arp._json(patch_receipt))
    arp._atomic_write(src / shared.READY, (KEY + '\n').encode())
    arp._atomic_write(work / 'fingerprint-diagnostics/source-final.json', arp._json(verified))
    arp._atomic_write(src / MARKER, arp._json(result))
    arp._atomic_write(work / RECEIPT, arp._json(result))
    (src / arp.IN_PROGRESS).unlink()
    current_identity, current_patches, current_lite = arp._load(
        repo, work / 'tooling/ungoogled-chromium', work / 'tooling/ungoogled-chromium-macos', 'macos')
    shared._previous_completed(src, current_identity, shared._names(current_patches, current_lite))
    require(arm.output_inventory(out) == inventory, 'migration changed output state')
    (src / shared.TRANSACTION).unlink()
    return result


def source_state(repo, work):
    src = work / 'src'
    receipt = json.loads(arm.read(work, RECEIPT))
    require(all(receipt.get(k) == v for k, v in identity().items()), 'migration receipt identity mismatch')
    require(arm.read(src, MARKER) == arm.read(work, RECEIPT), 'migration marker/receipt mismatch')
    require(recipes(repo) == receipt['recipes'], 'admitted 228 recipes changed')
    for name in (shared.TRANSACTION, arp.IN_PROGRESS):
        require(not (src / name).exists(), 'interrupted source migration')
    shared._ready(src, KEY)
    for name, digest in donor.DONOR_RECEIPTS.items():
        actual = ARCHIVED_SOURCE if name == 'fingerprint-diagnostics/source-final.json' else name
        require(sha(arm.read(work, actual)) == digest, 'historical donor receipt changed: ' + name)
    for name, digest in arm.PINNED_FILES.items():
        if name not in ('src/.chromix-restored-patches.json', 'fingerprint-diagnostics/source-final.json'):
            require(sha(arm.read(work, name)) == digest, 'protected source/GN/upstream changed')
    require(sha(arm.read(src, arp.MARKER)) == receipt['patch_receipt_sha256']
            and sha(arm.read(work, 'fingerprint-diagnostics/source-final.json')) == receipt['source_report_sha256'],
            '228 source receipts changed')
    require(arp.patch_selection.source_version(src) == arm.VERSION, 'source version changed')
    arm.preparation(work)
    arm.read(src, '.chromix-toolchain-ready')
    arm.read(src, '.chromix-domain-substituted')
    state = {name: sha(arm.read(src, name)) for name in arm.STATE_FILES}
    state['graph'] = arm.identity_hash({p.relative_to(src).as_posix(): sha(arm.read(src, p.relative_to(src).as_posix()))
                                      for p in sorted((src / 'out/Default').rglob('*.ninja'))})
    require(state['graph'] == receipt['state']['graph'], 'GN graph changed; regeneration forbidden')
    return state


def live_source(repo, work, output):
    require(not output.exists(), 'new source report required')
    original = json.loads(arm.read(work, 'fingerprint-diagnostics/source-final.json'))
    result = canonical(verifier.verify(work / 'src', repo, core=work / 'tooling/ungoogled-chromium',
                       tooling=work / 'tooling/ungoogled-chromium-macos', platform='macos'), original)
    require(result == original and result['patch_count'] == 228
            and arm.identity_hash(result['identity']) == SOURCE_IDENTITY
            and arm.identity_hash(result['outputs']) == SOURCE_OUTPUTS, '228 live source identity changed')
    output.write_bytes(arp._json(result))


def continuation(repo, work, stage, phase):
    require(phase in ('before', 'after') and 1 <= stage <= 8, 'invalid continuation stage/phase')
    require(os.environ.get('CHROMIX_BUILD_PROFILE') == 'fast' and os.environ.get('CHROMIX_USE_UPSTREAM_CACHE') == '1',
            'fast profile and mandatory restored cache required')
    for key in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'):
        require(re.fullmatch(r'[1-9][0-9]*', os.environ.get(key, '')), 'run/attempt required')
    receipt = json.loads(arm.read(work, RECEIPT))
    require(receipt['continuation_run'] == os.environ['GITHUB_RUN_ID']
            and receipt['provenance']['manifest'] == donor.expected_manifest(), 'run/donor isolation mismatch')
    state = source_state(repo, work)
    if phase == 'before':
        arm.native_state(repo, work)
        if stage == 1:
            require(state == receipt['state'], 'initial migrated Ninja state changed')
        else:
            arm.previous_handoff(work, CONTINUATION, stage, 12, identity(), state)
    output = work / 'fingerprint-diagnostics' / f'{CONTINUATION}-s{stage}-{phase}-attempt-{os.environ["GITHUB_RUN_ATTEMPT"]}.json'
    live_source(repo, work, output.with_name(output.stem + '-source.json'))
    result = dict(identity(), continuation_run=os.environ['GITHUB_RUN_ID'],
                  continuation_attempt=os.environ['GITHUB_RUN_ATTEMPT'], continuation_stage=stage,
                  logical_stage=12 + stage, phase=phase, state=state)
    output.write_bytes(arp._json(result))
    return result


def check_compile_plan(commands, src):
    compiled = []
    for command in commands:
        words = shlex.split(command)
        compiler = any(Path(word).name in ('clang', 'clang++', 'cc', 'c++', 'gcc', 'g++') for word in words)
        if compiler and '-c' in words:
            require(words.count('-c') == 1 and words.index('-c') + 1 < len(words), 'ambiguous compile command')
            target = (src / 'out/Default' / words[words.index('-c') + 1]).resolve()
            require(target in {(src / name).resolve() for name in SURFACE},
                    'compile plan includes unrelated source; no broad rebuild permitted')
            compiled.append(target)
    require(len(compiled) <= 2 and len(compiled) == len(set(compiled)), 'unexpected duplicate/broad compile plan')


def frozen_ninja(ninja, work, jobs, env):
    """Load the original graph through a non-generated entry, without touching out."""
    out = work / "src/out/Default"
    graphs = sorted(out.rglob("*.ninja"))
    for path in graphs:
        raw = arm.read(out, path.relative_to(out).as_posix())
        # A dry run cannot attest to dependencies introduced by future commands.
        require(not re.search(rb"(?m)^\s*dyndep\s*=", raw),
                "dynamic Ninja dependencies cannot use frozen-graph continuation")
    inventory = arm.output_inventory(out)
    diagnostics = Path(tempfile.mkdtemp(prefix="mac156-ninja-probes-", dir=work / "fingerprint-diagnostics"))
    with arm.preflight_source(work, diagnostics) as (scratch, sandbox), \
            tempfile.TemporaryDirectory(prefix="entry-", dir=diagnostics) as temporary:
        wrapper = Path(temporary) / "entry.ninja"
        wrapper.write_text("include build.ninja\n")
        command = [*sandbox, str(ninja), "-C", str(scratch / "out/Default"), "-f", str(wrapper)]

        probe_index = 0

        def inspect(arguments, probe_env=env):
            nonlocal probe_index
            probe_index += 1
            prefix = diagnostics / f"probe-{probe_index:03d}"
            result = subprocess.run([*command, *arguments], cwd=scratch, env=probe_env,
                                    capture_output=True, text=True, check=False, timeout=180)
            prefix.with_suffix(".stdout").write_text(result.stdout)
            prefix.with_suffix(".stderr").write_text(result.stderr)
            prefix.with_suffix(".json").write_bytes(arp._json({"arguments": arguments, "returncode": result.returncode}))
            require(result.returncode == 0, f"Ninja probe failed; see {prefix}")
            require(not result.stderr, f"Ninja graph inspection produced warnings; see {prefix}")
            return result.stdout

        forbidden, proof = arm.regeneration_proof(scratch / "out/Default",
                                               sorted((scratch / "out/Default").rglob("*.ninja")), wrapper, inspect)
        (diagnostics / "regeneration-proof.json").write_bytes(arp._json(proof))
        # Unlike -t commands, the dry-run plan includes validation and log dependencies.
        plan_env = dict(env, NINJA_STATUS="CHROMIX_FROZEN_PLAN ")
        plan = inspect(["-n", "-v", "-j", jobs, "chrome"], plan_env)
        planned = []
        for line in plan.splitlines():
            if line.startswith("CHROMIX_FROZEN_PLAN "):
                planned.append(line.removeprefix("CHROMIX_FROZEN_PLAN "))
            else:
                require(line.startswith("ninja: Entering directory ") or line == "ninja: no work to do.",
                        "unrecognized or multiline frozen Ninja command")
        require(not forbidden.intersection(planned), "GN/manifest regeneration in compile plan is forbidden")
        check_compile_plan(planned, scratch)
    difference = arm.inventory_difference(inventory, arm.output_inventory(out))
    (diagnostics / "original-out-difference.json").write_bytes(arp._json(difference))
    require(not difference, "Ninja preflight modified existing output state")
    with tempfile.TemporaryDirectory(prefix="compile-entry-", dir=diagnostics) as temporary:
        wrapper = Path(temporary) / "entry.ninja"
        wrapper.write_text("include build.ninja\n")
        # Only the approved original graph is compiled; scratch objects/logs are never published.
        return subprocess.run([str(ninja), "-C", str(out), "-f", str(wrapper), "-j", jobs, "chrome"],
                              cwd=work / "src", env=env, check=False).returncode


def compile_tree(repo, work, stage):
    require(stage is not None and 1 <= stage <= 8, 'compile stage required')
    state = source_state(repo, work)
    receipt = json.loads(arm.read(work, RECEIPT))
    require(receipt['continuation_run'] == os.environ.get('GITHUB_RUN_ID'), 'unadmitted compile')
    before = json.loads(arm.read(work, f'fingerprint-diagnostics/{CONTINUATION}-s{stage}-before-attempt-'
                                f'{os.environ.get("GITHUB_RUN_ATTEMPT", "local")}.json'))
    expected = dict(identity(), continuation_run=os.environ.get('GITHUB_RUN_ID'),
                    continuation_attempt=os.environ.get('GITHUB_RUN_ATTEMPT'), continuation_stage=stage,
                    logical_stage=12 + stage, phase='before', state=state)
    require(before == expected, 'same-attempt 228 source gate required')
    ninja = Path(shutil.which('ninja') or 'missing').resolve()
    require(arm.restore_ninja.host_identity() == ('macos', 'arm64')
            and arm.restore_ninja.binary_architectures(ninja, 'macos') == {'arm64'}
            and arm.restore_ninja.probe_version(ninja, work) == '1.13.2', 'native Ninja 1.13.2 required')
    jobs = os.environ.get('CHROMIX_JOBS', '')
    require(re.fullmatch(r'[1-9][0-9]*', jobs), 'positive compile jobs required')
    return frozen_ninja(ninja, work, jobs, {k: v for k, v in os.environ.items() if not k.startswith('DYLD_')})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('metadata', 'admit', 'check', 'compile'), required=True)
    for flag in ('repo', 'workdir', 'previous-repo', 'origin', 'download-report', 'report'):
        parser.add_argument('--' + flag, type=Path)
    parser.add_argument('--stage', type=int)
    parser.add_argument('--phase', choices=('before', 'after'))
    args = parser.parse_args(argv)
    try:
        if args.mode == 'metadata':
            require(args.report is not None, 'metadata report required')
            return donor.main(['--mode', 'metadata', '--report', str(args.report)])
        repo = (args.repo or Path(__file__).resolve().parents[1]).resolve()
        require(args.workdir is not None, 'workdir required')
        work = args.workdir.resolve()
        if args.mode == 'admit':
            require(all((args.previous_repo, args.origin, args.download_report)), 'exact migration provenance required')
            migrate(work, args.previous_repo.resolve(), repo, args.origin, args.download_report)
        elif args.mode == 'check':
            require(args.stage is not None and args.phase is not None, 'stage and phase required')
            continuation(repo, work, args.stage, args.phase)
        else:
            return compile_tree(repo, work, args.stage)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f'mac156 HEVC source migration rejected: {error}; restore exact stage3 donor again', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
