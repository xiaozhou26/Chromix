"""Real pre-GN failure metadata, exact Cargo output and tool-only donor admission."""
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import windows157_tools as tools
import windows157_crubit_resume as resume
import gen_windows_arm64_workflow as generator
from test_windows157 import pe_dll, put, host_tree, FILES

HISTORICAL_AUDIT = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/windows157_crubit_checkpoint.json.gz').read_bytes()))
AUDIT = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/windows157_handle_native.json.gz').read_bytes()))['latest_checkpoint']
SOURCE = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/windows157_crubit_source.json.gz').read_bytes()))
NOW = datetime(2026, 11, 19, 16, tzinfo=timezone.utc)


class Client:
    def __init__(self):
        self.run = deepcopy(AUDIT['run'])
        self.jobs = deepcopy(AUDIT['jobs']['jobs'])
        self.artifacts = deepcopy(AUDIT['artifacts']['artifacts'])

    def get(self, path):
        assert path == f'/actions/runs/{resume.RUN}'
        return self.run

    def items(self, path, key):
        return self.jobs if key == 'jobs' else self.artifacts


def executable(path):
    data = bytearray(pe_dll())
    struct.pack_into('<H', data, 86, 2)
    return put(path, data)


def message(src, path):
    manifest = src / tools.CRUBIT_ROOT / 'lib/third_party/crubit' / tools.CRUBIT_BIN_MANIFEST
    return dict(reason='compiler-artifact', target={'name': 'cc_bindings_from_rs', 'kind': ['bin']},
                profile={'test': False}, manifest_path=str(manifest), executable=str(path), filenames=[str(path)], fresh=True)


def cargo_json(data):
    return json.dumps(data) + '\n' + json.dumps({'reason': 'build-finished', 'success': True}) + '\n'


def test_actual_rust_sources_distinguish_host_build_install_and_target_dir():
    for source in HISTORICAL_AUDIT['sources'].values():
        assert hashlib.sha256(source['text'].encode()).hexdigest() == source['sha256']
    rust = HISTORICAL_AUDIT['sources']['tools/rust/build_rust.py']['text']
    assert "self._env['CARGO_HOME'] = CARGO_HOME_DIR" in rust
    assert "xpy.run('install', xpy_args)" in rust
    bindgen = HISTORICAL_AUDIT['sources']['tools/rust/build_bindgen.py']['text']
    assert "f'--target={RustTargetTriple()}'" in bindgen
    assert "os.path.join(build_dir, RustTargetTriple(), 'release'" in bindgen
    crubit = HISTORICAL_AUDIT['sources']['tools/rust/build_crubit.py']['text']
    assert "release_dir = os.path.join(target_dir, 'release')" in crubit
    assert tools.CRUBIT_BIN_SHA256 == HISTORICAL_AUDIT['sources'][tools.CRUBIT_BIN_MANIFEST]['sha256']
    assert any('source ready: ' + '|'.join((tools.VERSION, tools.CORE, tools.WINDOWS, resume.PREPARE_KEY)) in l
               for l in HISTORICAL_AUDIT['phase_log'])
    assert any('Finished `release` profile' in l for l in HISTORICAL_AUDIT['phase_log'])
    assert any('out\\crubit\\release\\cc_bindings_from_rs.exe' in l for l in HISTORICAL_AUDIT['phase_log'])


def test_explicit_target_json_output_and_wrong_arch_refused(tmp_path):
    src = tmp_path.resolve()
    target = src / 'out/crubit'
    exact = executable(target / tools.HOST_TRIPLE / 'release/cc_bindings_from_rs.exe')
    assert tools.cargo_output(cargo_json(message(src, exact)), src, target) == exact
    for path in (target / 'release/cc_bindings_from_rs.exe', target / 'aarch64-pc-windows-msvc/release/cc_bindings_from_rs.exe',
                 src / 'third_party/rust-toolchain/bin/cc_bindings_from_rs.exe', target / tools.HOST_TRIPLE / 'release/other.exe'):
        executable(path)
        with pytest.raises(ValueError, match='unverified'):
            tools.cargo_output(cargo_json(message(src, path)), src, target)
    bad = bytearray(exact.read_bytes())
    struct.pack_into('<H', bad, 68, 0xAA64)
    exact.write_bytes(bad)
    with pytest.raises(ValueError, match='AMD64'):
        tools.cargo_output(cargo_json(message(src, exact)), src, target)


@pytest.mark.parametrize('mutation', ['manifest', 'name', 'kind', 'test', 'duplicate', 'failure', 'missing'])
def test_cargo_success_is_not_enough(tmp_path, mutation):
    src = tmp_path.resolve()
    target = src / 'out/crubit'
    path = executable(target / tools.HOST_TRIPLE / 'release/cc_bindings_from_rs.exe')
    data = message(src, path)
    if mutation == 'manifest': data['manifest_path'] = str(src / 'unknown/Cargo.toml')
    if mutation == 'name': data['target']['name'] = 'another_binary'
    if mutation == 'kind': data['target']['kind'] = ['lib']
    if mutation == 'test': data['profile']['test'] = True
    payload = cargo_json(data)
    if mutation == 'duplicate': payload = json.dumps(data) + '\n' + payload
    if mutation == 'failure': payload = payload.replace('"success": true', '"success": false')
    if mutation == 'missing': path.unlink()
    with pytest.raises(ValueError): tools.cargo_output(payload, src, target)


def test_prepare_real_cargo_layout_installs_verified_bytes_without_clean(tmp_path):
    work = tmp_path.resolve()
    src = host_tree(work / 'src')
    put(src / 'out/Chromix/args.gn', 'target_cpu="x64"\nhost_cpu="x64"\nchrome_pgo_phase=2\n')
    root = src / tools.CRUBIT_ROOT
    (root / 'bin/cc_bindings_from_rs.exe').unlink()
    put(root / 'lib/third_party/crubit' / tools.CRUBIT_BIN_MANIFEST, HISTORICAL_AUDIT['sources'][tools.CRUBIT_BIN_MANIFEST]['text'])
    for name in tools.TOOLING: put(work / 'tooling/ungoogled-chromium-windows' / name, FILES['windows/' + name]['text'])
    for name in ('rustc.exe', 'cargo.exe'): executable(src / 'third_party/rust-toolchain/bin' / name)
    target = src / 'out/crubit'
    retained = put(target / 'cargo_home/registry/retained', b'cache')
    result_path = executable(target / tools.HOST_TRIPLE / 'release/cc_bindings_from_rs.exe')
    seen = []
    def run(argv, **kwargs):
        seen.append(argv)
        if '--target' in argv:
            assert argv[argv.index('--target') + 1] == tools.HOST_TRIPLE
            assert '--message-format=json-render-diagnostics' in argv
            assert kwargs['cwd'] == src
            assert kwargs['env']['RUSTC'] == str(src / 'third_party/rust-toolchain/bin/rustc.exe')
            assert 'CARGO_BUILD_TARGET' not in kwargs['env'] and 'CARGO_ENCODED_RUSTFLAGS' not in kwargs['env']
            return SimpleNamespace(stdout=cargo_json(message(src, result_path)))
        assert argv[-1] == '--help'
        return SimpleNamespace(stdout='')
    runtime = dict(platform='win32', bits=64, process_machine=0x8664, native_machine=0x8664)
    with mock.patch.object(tools.metagen, 'runtime_info', return_value=runtime), \
            mock.patch.dict(os.environ, {'CARGO_BUILD_TARGET': 'aarch64-pc-windows-msvc', 'CARGO_ENCODED_RUSTFLAGS': 'wrong'}), \
            mock.patch.object(tools.subprocess, 'run', side_effect=run):
        tools.prepare(src, work)
        tools.prepare(src, work)
    assert len(seen) == 3
    assert (root / 'bin/cc_bindings_from_rs.exe').read_bytes() == result_path.read_bytes()
    assert retained.read_bytes() == b'cache'
    assert not any('clean' in argv for argv in seen)


def test_real_latest_two_volumes_and_producer_attempt():
    assert resume.metadata(Client(), NOW) == resume.manifest()
    assert [a['id'] for a in resume.manifest()['artifacts']] == [14559964146, 14561755654]


@pytest.mark.parametrize('mutation', ['attempt', 'sha', 'job', 'upload', 'expired', 'size', 'digest', 'extra', 'missing', 'later'])
def test_fixed_metadata_rejects_cross_run_attempt_and_incomplete_upload(mutation):
    client = Client()
    if mutation == 'attempt': client.run['run_attempt'] = 2
    if mutation == 'sha': client.run['head_sha'] = 'f' * 40
    job = next(j for j in client.jobs if j['id'] == resume.JOB)
    if mutation == 'job': job['id'] += 1
    if mutation == 'upload': next(s for s in job['steps'] if s['name'] == 'Upload tree part 2')['conclusion'] = 'failure'
    if mutation == 'expired': client.artifacts[0]['expired'] = True
    if mutation == 'size': client.artifacts[0]['size_in_bytes'] += 1
    if mutation == 'digest': client.artifacts[0]['digest'] = 'sha256:' + '0' * 64
    if mutation == 'extra': client.artifacts.append(dict(client.artifacts[0], id=12, name='win-x64-157-tree-s2-attempt-1-part1'))
    if mutation == 'missing': client.artifacts.pop()
    if mutation == 'later': next(j for j in client.jobs if j['name'] == 'Windows157 x64 stage 2')['conclusion'] = 'success'
    with pytest.raises(ValueError): resume.metadata(client, NOW)


def test_expiry_stops_without_fresh_fallback():
    with pytest.raises(ValueError, match='expiry'):
        resume.metadata(Client(), datetime(2026, 12, 4, tzinfo=timezone.utc))


def test_authenticated_download_requires_both_contiguous_volumes():
    origin = resume.manifest()
    report = dict(status='success', phase='complete', publication='published', repository=resume.REPOSITORY,
                  head_sha=resume.DONOR_SHA, run_id=resume.RUN, artifacts=origin['artifacts'],
                  volumes=[dict(name=f'tree.7z.{i:03d}', artifact_id=p[0], size_in_bytes=100,
                                sha256='a' * 64) for i, p in enumerate(resume.PARTS, 1)], total_size_in_bytes=200)
    resume.checked_download(origin, report)
    for key, bad in [('publication', 'staged'), ('head_sha', 'f' * 40), ('volumes', report['volumes'][:1]),
                     ('total_size_in_bytes', 199), ('artifacts', origin['artifacts'][:1])]:
        with pytest.raises(ValueError): resume.checked_download(origin, dict(report, **{key: bad}))


@pytest.fixture(scope='module')
def recipe(tmp_path_factory):
    directory = tmp_path_factory.mktemp('crubit-recipe').resolve()
    view = directory / resume.VIEW
    resume.recipe_view(ROOT, view, 'git')
    resume.view_identity(ROOT, view, 'git')
    return view


def test_linux_only_main_change_preserves_exact_windows_donor_recipe(recipe):
    donor = json.loads(subprocess.check_output(['git', '-C', str(ROOT), 'show', resume.RECIPE_SHA + ':build/patch-selection-157.json']))
    current = json.loads((ROOT / 'build/patch-selection-157.json').read_text())
    assert donor != current
    assert resume.windows_recipe(donor) == resume.windows_recipe(current)
    actual = subprocess.check_output([sys.executable, str(recipe / 'tools/patch_selection.py'), '--repo', str(recipe),
                                      '--platform', 'windows', '--key', 'windows'], text=True).strip()
    assert actual == resume.PREPARE_KEY
    assert (recipe / resume.HELPER).read_bytes() == (ROOT / resume.HELPER).read_bytes()
    assert (recipe / 'build/windows/ci-stage.ps1').read_bytes().replace(b'\r\n', b'\n') == subprocess.check_output(
        ['git', '-C', str(ROOT), 'show', resume.RECIPE_SHA + ':build/windows/ci-stage.ps1'])
    assert b'\r\n' in (recipe / 'build/windows/lite-tarball-files/v8/test/torque/test-torque.tq').read_bytes()


def test_complete_real_affected_sources_and_all_232_patches(recipe, tmp_path):
    src = tmp_path.resolve() / 'src'
    for name, text in SOURCE['files'].items(): put(src / name, text)
    assert len(resume.full_source(ROOT, src)) == 176
    report = tmp_path.resolve() / 'source.json'
    subprocess.run([sys.executable, str(recipe / 'tools/verify_patch_stack.py'), '--repo', str(recipe), '--src', str(src),
                    '--platform', 'windows', '--output', str(report)], check=True, capture_output=True)
    assert json.loads(report.read_text())['patch_count'] == 232
    # A change outside a patch hunk must fail complete-file proof too.
    path = src / 'content/renderer/render_thread_impl.cc'
    path.write_bytes(path.read_bytes() + b'\n// unrelated injected source\n')
    with pytest.raises(ValueError, match='complete affected-source'): resume.full_source(ROOT, src)


def test_recipe_tamper_is_not_admitted(recipe):
    helper = recipe / resume.HELPER
    before = helper.read_bytes()
    try:
        helper.write_bytes(before + b'# changed\n')
        with pytest.raises(ValueError, match='helper changed'): resume.view_identity(ROOT, recipe, 'git')
    finally:
        helper.write_bytes(before)


def test_dedicated_workflow_cannot_start_fresh_or_choose_other_donor():
    workflow = generator.crubit_resume_workflow()
    assert set(workflow['on']['workflow_dispatch']['inputs']) == {'compile_jobs'}
    assert set(workflow['on']) == {'workflow_dispatch'}
    for i in range(1, 13):
        steps = workflow['jobs'][f'build-{i}']['steps']
        stage = next(s for s in steps if s.get('id') == 'stage')
        assert '-CrubitRecovery' in stage['run'] and '-FromSynced' in stage['run']
        assert 'RESUME_RUN_ID' not in stage.get('env', {})
        assert next(s for s in steps if s.get('uses') == 'actions/checkout@v4')['with']['fetch-depth'] == 0
    restore = next(s for s in workflow['jobs']['build-1']['steps'] if s.get('name') == 'Restore fixed Windows157 Crubit checkpoint')
    assert 'if' not in restore
    assert restore['run'].index('--phase metadata') < restore['run'].index('download_windows_snapshot.py') < restore['run'].index('--phase admit')
    assert '--phase initialize' not in restore['run']
    assert (ROOT / f'.github/workflows/{resume.WORKFLOW}.yml').read_text() == generator.render_crubit_resume()
    ci = (ROOT / 'build/windows/ci-stage.ps1').read_text()
    assert ci.index('--phase view') < ci.index('$Repo = "C:\\c\\windows157-crubit-recipe"')
    assert 'historical fresh entries cannot change versions' in ci
    assert '--phase view' in ci and '$FromSynced' in ci


def test_precise_pre_gn_phase_rejects_post_gn_and_preserves_output(recipe, tmp_path):
    import shutil
    import windows157_fresh as fresh
    import repair_windows_midl as midl
    import merge_gn_args
    root = tmp_path.resolve()
    view = root / resume.VIEW
    shutil.copytree(recipe, view)
    work = root / 'chromix'
    src = work / 'src'
    host_tree(src)
    # The real donor failed before domain substitution.
    put(src / 'v8/tools/metagen/compile_flags.py', FILES['v8/tools/metagen/compile_flags.py']['text'])
    put(work / fresh.RECEIPT, json.dumps(fresh.identity('x64')))
    markers = {'.chromix-source-unpacked': fresh.VERSION, '.chromix-ungoogled-core': fresh.CORE,
               '.chromix-ungoogled-windows': fresh.WINDOWS, '.chromix-binaries-pruned': fresh.CORE,
               '.chromix-patches': resume.PREPARE_KEY, '.chromix-patch-selection': resume.PREPARE_KEY,
               '.chromix-source-ready': '|'.join((fresh.VERSION, fresh.CORE, fresh.WINDOWS, resume.PREPARE_KEY))}
    for name, value in markers.items(): put(src / name, value)
    put(src / 'out/Chromix/args.gn', 'target_cpu="x64"\nhost_cpu="x64"\nchrome_pgo_phase=2\n')
    raw = (ROOT / 'tools/tests/fixtures/windows153_midl.py').read_bytes().replace(
        b'chromium-review.9oo91esource.qjz9zk', b'chromium-review.googlesource.com')
    repaired = raw.replace(midl.BEFORE, midl.AFTER).replace(midl.COMPARE_BEFORE, midl.COMPARE_AFTER)
    put(src / midl.SCRIPT, repaired)
    executable(src / 'third_party/rust-toolchain/bin/bindgen.exe')
    (src / tools.CRUBIT_ROOT / 'bin/cc_bindings_from_rs.exe').unlink()
    cache = put(src / 'out/crubit/cargo_home/registry/preserved', b'cargo cache')
    put(src / 'chrome/build/win64.pgo.txt', tools.PGO['x64'])
    assert resume.phase_contract(work) == markers
    before = resume.tree_state(src / 'out')
    for relative in ('out/Chromix/build.ninja', 'out/Chromix/gn.exe', '.chromix-domain-substituted',
                     '.chromix-patch-in-progress'):
        path = put(src / relative, b'invalid phase')
        with pytest.raises(ValueError): resume.phase_contract(work)
        path.unlink()
    assert resume.tree_state(src / 'out') == before and cache.read_bytes() == b'cargo cache'
    source = src / '.chromix-source-ready'
    value = source.read_text()
    source.write_text(value.replace(resume.PREPARE_KEY, '0' * 64))
    with pytest.raises(ValueError, match='marker'): resume.phase_contract(work)
    source.write_text(value)


def test_consumer_cannot_be_generic_fresh_wrong_sha_or_other_attempt():
    env = dict(GITHUB_REPOSITORY=resume.REPOSITORY, GITHUB_REF='refs/heads/main',
               GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_WORKFLOW_REF=f'{resume.REPOSITORY}/.github/workflows/{resume.WORKFLOW}.yml@refs/heads/main',
               GITHUB_RUN_ID='555', GITHUB_RUN_ATTEMPT='1', GITHUB_JOB='build-1', GITHUB_SHA='a' * 40)
    with mock.patch.dict(os.environ, env): assert resume.consumer() == 'a' * 40
    for key, value in [('GITHUB_RUN_ID', str(resume.RUN)), ('GITHUB_SHA', resume.DONOR_SHA),
                       ('GITHUB_REF', 'refs/heads/other'), ('GITHUB_EVENT_NAME', 'push'),
                       ('GITHUB_WORKFLOW_REF', f'{resume.REPOSITORY}/.github/workflows/build-win-x64-157-fresh.yml@refs/heads/main'),
                       ('GITHUB_JOB', 'build-13'), ('GITHUB_RUN_ATTEMPT', '0')]:
        with mock.patch.dict(os.environ, dict(env, **{key: value})), pytest.raises(ValueError): resume.consumer()


def test_reviewed_code_tuple_and_pinned_source_proof_are_exact():
    old = subprocess.check_output(['git', '-C', str(ROOT), 'show', resume.DONOR_SHA + ':' + resume.HELPER])
    assert (hashlib.sha256(old).hexdigest(), hashlib.sha256((ROOT / resume.HELPER).read_bytes()).hexdigest()) == resume.HELPER_TUPLE
    assert hashlib.sha256((ROOT / resume.PROOF).read_bytes()).hexdigest() == resume.PROOF_SHA256
    for name, expected in resume.RECOVERY_CODE.items():
        assert hashlib.sha256((ROOT / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest() == expected


def test_cargo_install_does_not_touch_arm64_cache_path():
    stage = (ROOT / 'build/windows/ci-stage.ps1').read_text()
    start = stage.index('if ($Fresh157) {', stage.index('if ($RestoredUpstream) {\n    python'))
    assert '--phase prepare' in stage[start:stage.index('  $gn = ', start)]
    assert '$Fresh157 = $FreshNative' in stage and '-and $Arch -eq "x64"' in stage
    source = (ROOT / 'tools/windows157_tools.py').read_text()
    assert 'if restored or args.arch != "x64":' in source
    assert 'if not executable.exists():' in source


def test_frozen_recipe_comparison_rejects_windows_change_not_linux_change():
    donor = json.loads(subprocess.check_output(['git', '-C', str(ROOT), 'show', resume.RECIPE_SHA + ':build/patch-selection-157.json']))
    changed = deepcopy(donor)
    changed['platforms']['linux']['downstream']['note-test-only'] = 'different platform'
    assert resume.windows_recipe(changed) == resume.windows_recipe(donor)
    changed['platforms']['windows']['commit'] = 'f' * 40
    assert resume.windows_recipe(changed) != resume.windows_recipe(donor)



def test_current_main_base_includes_reviewed_platform_inputs_without_wildcards():
    assert resume.MAIN_BASE == "f80cfcc644cb3de026971e7585951649effbd877"
    assert resume.REVIEWED_PLATFORM_INPUTS == {}
    base = resume.git_tree('git', ROOT, resume.MAIN_BASE)
    resume.reviewed_changes('git', ROOT, resume.MAIN_BASE, base, base)
    for name in ('tools/macos_restored_generators.py', 'tools/linux157_build.py',
                 'tools/tests/test_unreviewed.py', 'build/windows/prepare-ungoogled.ps1'):
        changed = dict(base, **{name: ('100644', 'blob', 'f' * 40)})
        with pytest.raises(ValueError, match='outside reviewed'):
            resume.reviewed_changes('git', ROOT, resume.MAIN_BASE, base, changed)
