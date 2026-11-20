"""Exact stage3 metadata and real full Windows232->235 compiled-source replay."""
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest import mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import windows157_compiled_resume as recovery
import apply_restored_patches as patches
import gen_windows_arm64_workflow as generator

FIXTURE = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/windows157_compiled_stage3.json.gz').read_bytes()))
RAW = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/chromium157_patch_sources.json.gz').read_bytes()))
NOW = datetime(2026, 11, 20, 9, tzinfo=timezone.utc)


class Client:
    def __init__(self): self.data = deepcopy(FIXTURE['metadata'])
    def get(self, path): return self.data['run']
    def items(self, path, key): return self.data[key][key]


def source_tree(root):
    for name, text in FIXTURE['sources'].items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
    return root


def real_stack():
    _, raw = recovery.validate_selection(ROOT, recovery.load(ROOT))
    listed = set(RAW['domain_lists']['windows'].splitlines())
    rules = patches._rules(RAW['domain_regex'].encode())
    return [(name, *patches.transform_patch(data, listed, rules)) for name, data in raw]


def test_exact_latest_stage3_two_parts_and_four_successful_upload_slots():
    m = recovery.load(ROOT)
    result = recovery.metadata(Client(), m, NOW)
    assert (result['run_id'], result['job_id'], result['stage'], result['attempt']) == (44591948738, 136097857343, 3, 1)
    assert [a['id'] for a in result['artifacts']] == [14611575270, 14611525307]
    assert [a['size_in_bytes'] for a in result['artifacts']] == [9663676664, 2338891455]
    assert m['source_receipt']['patch_count'] == 232
    assert m['parent_admission']['consumer_run_id'] == '44591948738'


@pytest.mark.parametrize('mutation', ['run', 'sha', 'attempt', 'job', 'upload', 'digest', 'size', 'expiry', 'missing', 'extra', 'later'])
def test_metadata_never_falls_back_to_earlier_or_incomplete_tree(mutation):
    c = Client();m = recovery.load(ROOT)
    job = next(j for j in c.data['jobs']['jobs'] if j['id'] == m['job_id'])
    tree = next(a for a in c.data['artifacts']['artifacts'] if a['id'] == 14611575270)
    if mutation == 'run': c.data['run']['id'] -= 1
    if mutation == 'sha': c.data['run']['head_sha'] = '0' * 40
    if mutation == 'attempt': c.data['run']['run_attempt'] = 2
    if mutation == 'job': job['id'] -= 1
    if mutation == 'upload': next(s for s in job['steps'] if s['name'] == 'Upload tree part 4')['conclusion'] = 'failure'
    if mutation == 'digest': tree['digest'] = 'sha256:' + '0' * 64
    if mutation == 'size': tree['size_in_bytes'] -= 1
    if mutation == 'expiry': tree['expired'] = True
    if mutation == 'missing': c.data['artifacts']['artifacts'].remove(tree)
    if mutation == 'extra': c.data['artifacts']['artifacts'].append(dict(tree, id=1))
    if mutation == 'later': next(j for j in c.data['jobs']['jobs'] if j['name'] == 'Windows157 x64 stage 4')['conclusion'] = 'success'
    with pytest.raises(ValueError): recovery.metadata(c, m, NOW)
    with pytest.raises(ValueError): recovery.metadata(Client(), m, datetime(2027, 1, 1, tzinfo=timezone.utc))


def test_download_requires_full_latest_two_volume_proof():
    m = recovery.load(ROOT);origin = recovery.expected(m)
    download = dict(status='success', phase='complete', publication='published', repository=m['repository'],
                    run_id=m['run_id'], head_sha=m['head_sha'], artifacts=origin['artifacts'],
                    volumes=[dict(name=f'tree.7z.{i:03d}', artifact_id=a['id'], size_in_bytes=100, sha256='a'*64)
                             for i,a in enumerate(origin['artifacts'],1)], total_size_in_bytes=200)
    recovery.checked_download(m, origin, download)
    for key,value in [('run_id',44576227466),('publication','staged'),('artifacts',origin['artifacts'][:1]),
                      ('total_size_in_bytes',199),('volumes',download['volumes'][:1])]:
        with pytest.raises(ValueError): recovery.checked_download(m, origin, dict(download,**{key:value}))


def test_exact_root224_old232_prefix_and_three_new_files():
    m = recovery.load(ROOT);sel,raw = recovery.validate_selection(ROOT,m)
    assert len(raw)==235 and sel['patches'][:232]==m['source_receipt']['identity']['series']['patches']
    assert [Path(n).name[:4] for n,_ in raw[-3:]]==['0236','0237','0238']
    assert not any(Path(n).name.startswith(('0239','0240','0241')) for n,_ in raw)
    for mutation in ('prefix','addition','root'):
        changed=deepcopy(m)
        if mutation=='prefix':changed['source_receipt']['identity']['series']['patches'][0]['sha256']='0'*64
        if mutation=='addition':changed['additions'][0]['target']='other.cc'
        if mutation=='root':changed['root_series_sha256']='0'*64
        with pytest.raises(ValueError):recovery.validate_selection(ROOT,changed)


def test_real_windows232_and235_full_reverse_forward_preserves_outputs(tmp_path):
    src=source_tree(tmp_path.resolve()/'src');m=recovery.load(ROOT)
    recovery.verify_hashes(src,m['source_receipt']['outputs'])
    for n,b in [('out/Chromix/args.gn',b'chrome_pgo_phase=2'),('out/Chromix/.ninja_log',b'# ninja log v5\n'),
                ('out/Chromix/obj/keep.obj',b'compiled object'),('out/Chromix/build.ninja',b'build chrome: phony')]:
        p=src/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b)
    state=recovery.output_state(src/'out')
    before,revised=recovery.private_migration(src,real_stack())
    changed={n for n,(raw,_) in before.items() if (raw.replace(b'\r\n',b'\n') if raw is not None else None)!=revised[n]}
    assert changed==set(recovery.TARGETS)
    for pin in m['additions']:assert recovery.digest(revised[pin['target']])==pin['after']
    assert recovery.output_state(src/'out')==state
    assert not (src/recovery.PENDING).exists()
    with pytest.raises(ValueError,match='both transaction'):recovery.publish_sources(src,before,revised)
    (src/recovery.PENDING).write_text('pending');(src/recovery.BLOCKER).write_text('pending')
    recovery.publish_sources(src,before,revised)
    recovery.verify_hashes(src,{a['target']:a['after'] for a in m['additions']})
    assert recovery.output_state(src/'out')==state
    assert (src/recovery.PENDING).exists() and (src/recovery.BLOCKER).exists()
    again,_=recovery.private_migration(source_tree(tmp_path.resolve()/'again'),real_stack())
    assert len(again)==len(before)


def test_real_tamper_outside_hunk_is_caught_by_full_preimage(tmp_path):
    src=source_tree(tmp_path.resolve()/'src');m=recovery.load(ROOT)
    name='third_party/blink/renderer/core/timezone/timezone_controller.cc'
    path=src/name;path.write_bytes(path.read_bytes()+b'\n// injected outside hunk\n')
    with pytest.raises(ValueError,match='preimage'):recovery.verify_hashes(src,m['source_receipt']['outputs'])


def test_partial_atomic_publication_keeps_both_blockers(tmp_path):
    src=source_tree(tmp_path.resolve()/'src');before,revised=recovery.private_migration(src,real_stack())
    (src/recovery.PENDING).write_text('pending');(src/recovery.BLOCKER).write_text('pending')
    original=recovery.atomic_bytes;calls=[]
    def fail(path,*args,**kwargs):
        calls.append(path)
        if len(calls)==2:raise OSError('simulated publication failure')
        return original(path,*args,**kwargs)
    with mock.patch.object(recovery,'atomic_bytes',side_effect=fail),pytest.raises(OSError):
        recovery.publish_sources(src,before,revised)
    assert (src/recovery.PENDING).exists() and (src/recovery.BLOCKER).exists()
    assert not (src/recovery.RECEIPT).exists()


def test_output_hash_detects_same_size_same_mtime_tamper(tmp_path):
    root=tmp_path.resolve();p=root/'keep.obj';p.write_bytes(b'aaaa');before=recovery.output_state(root);stamp=p.stat()
    p.write_bytes(b'bbbb');os.utime(p,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    assert recovery.output_state(root)!=before


def test_stage4_to12_workflow_preserves_latest_and_native_acceptance():
    w=generator.compiled_resume_workflow()
    assert set(w['on'])=={'workflow_dispatch'}
    assert set(w['on']['workflow_dispatch']['inputs'])=={'compile_jobs'}
    assert set(w['jobs'])=={*(f'build-{i}' for i in range(4,13)),'complete'}
    assert w['jobs']['complete']['needs']==[f'build-{i}' for i in range(4,13)]
    for i in range(4,13):
        j=w['jobs'][f'build-{i}'];assert j['runs-on']=='windows-2022'
        if i==4:assert 'needs' not in j
        else:assert j['needs']==f'build-{i-1}' and "outputs.finished != 'true'" in j['if']
        stage=next(s for s in j['steps'] if s.get('id')=='stage')
        assert f'-StageIndex {i}' in stage['run'] and '-CompiledRecovery' in stage['run'] and '-FromSynced' in stage['run']
        save=next(s for s in j['steps'] if s.get('name')=='Ensure build tree snapshot')
        assert 'snapshot_safe' in save['if'] and 'always()' in save['if']
        for s in j['steps']:
            if s.get('name','').startswith('Upload tree part'):assert f'compiled-tree-s{i}-' in s['with']['name']
    restore=next(s for s in w['jobs']['build-4']['steps'] if s.get('name')=='Restore and migrate latest compiled stage3 checkpoint')
    assert '--phase migrate' in restore['run'] and 'windows157_compiled_resume.py' in restore['run']
    assert 'windows157_crubit_resume.py' not in restore['run']
    ci=(ROOT/'build/windows/ci-stage.ps1').read_text()
    assert 'Verify-FinalBundle' in ci and 'fingerprint_acceptance.py' in ci
    assert 'if (-not $CompiledRecovery)' in ci
    assert '$Fresh157 -and -not $CompiledRecovery' in ci
    assert (ROOT/'.github/workflows/build-win-x64-157-compiled-resume.yml').read_text()==generator.render_compiled_resume()


def test_powershell_parses_all_windows_entrypoints():
    pwsh=Path('/tmp/chromix-new-pwsh/pwsh')
    if not pwsh.exists():pytest.skip('PowerShell unavailable')
    script='$bad=0;Get-ChildItem build/windows/*.ps1|ForEach-Object {$t=$null;$e=$null;[System.Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$t,[ref]$e)|Out-Null;if($e.Count){$e;$bad++}};if($bad){exit 1}'
    subprocess.run([str(pwsh),'-NoLogo','-NoProfile','-Command',script],cwd=ROOT,check=True,capture_output=True)


def test_full_compiled_transaction_preserves_pgo_gn_objects_and_commits235(tmp_path):
    import struct
    import merge_gn_args
    from test_windows_metagen_bindings import pe_dll
    from test_windows157 import FILES, put
    m=recovery.load(ROOT);root=tmp_path.resolve();work=root/'chromix';src=source_tree(work/'src')
    from test_windows157 import sources
    import repair_windows_midl as midl
    sources(src,domain=True)
    payload=(ROOT/'tools/tests/fixtures/windows153_midl.py').read_bytes()
    payload=payload.replace(midl.BEFORE,midl.AFTER).replace(midl.COMPARE_BEFORE,midl.COMPARE_AFTER)
    put(src/'build/toolchain/win/midl.py',payload)
    view=root/recovery.VIEW;recovery.recipe_view(ROOT,view,'git')
    core=work/'tooling/ungoogled-chromium';tooling=work/'tooling/ungoogled-chromium-windows'
    put(core/'flags.gn',FIXTURE['core_flags']);put(tooling/'flags.windows.gn',FILES['windows/flags.windows.gn']['text'])
    put(core/'domain_regex.list',RAW['domain_regex']);put(tooling/'domain_substitution.list',RAW['domain_lists']['windows'])
    overlay=('# Windows157 fresh native build; never disable PGO for tarball sources.\nchrome_pgo_phase = 2\nhost_cpu = "x64"\n'
             f'v8_metagen_libclang_so = {json.dumps((src/"third_party/llvm-build/Release+Asserts/bin/libclang.dll").as_posix())}\n'
             f'v8_metagen_libclang_bindings_dir = {json.dumps((src/"third_party/llvm-build/Release+Asserts/bindings/python").as_posix())}\n')
    put(work/'windows157-native.gn',overlay)
    order=[];values={}
    for p in [core/'flags.gn',tooling/'flags.windows.gn',view/'build/args.windows.gn',work/'windows157-native.gn']:
        keys,parsed=merge_gn_args.parse(p);order.extend(k for k in keys if k not in order);values.update(parsed)
    put(src/'out/Chromix/args.gn','\n'.join([merge_gn_args.GENERATED_HEADER,*[values[k] for k in order]])+'\n')
    pe=bytearray(pe_dll());struct.pack_into('<H',pe,86,2)
    for n in ['out/Chromix/gn.exe','third_party/ninja/ninja.exe','third_party/rust-toolchain/bin/bindgen.exe',
              recovery.tools.CRUBIT_ROOT+'/bin/cc_bindings_from_rs.exe',
              'third_party/llvm-build/Release+Asserts/bin/clang-cl.exe',
              'third_party/llvm-build/Release+Asserts/bin/llvm-profdata.exe']:put(src/n,pe)
    put(src/'out/Chromix/build.ninja','build chrome: phony\n');put(src/'out/Chromix/.ninja_log','# ninja log v5\n1\t2\t3\tobj/keep.obj\tdeadbeef\n')
    put(src/'out/Chromix/.ninja_deps',b'# ninjadeps\n\x04\x00\x00\x00');put(src/'out/Chromix/obj/keep.obj',b'preserved object')
    profile=recovery.tools.PGO['x64'];put(src/'chrome/build/win64.pgo.txt',profile+'\n');put(src/'chrome/build/pgo_profiles'/profile,b'profile fixture')
    put(src/'chrome/build/pgo_profiles'/(profile+'.chromix-sha256'),recovery.digest(b'profile fixture')+'\n')
    markers={'.chromix-source-unpacked':m['version'],'.chromix-ungoogled-core':m['core'],'.chromix-ungoogled-windows':m['windows'],
             '.chromix-binaries-pruned':m['core'],'.chromix-domain-substituted':m['core'],'.chromix-patches':m['old_preparation_key'],
             '.chromix-patch-selection':m['old_preparation_key'],'.chromix-source-ready':'|'.join((m['version'],m['core'],m['windows'],m['old_preparation_key']))}
    for n,v in markers.items():put(src/n,v+'\n')
    put(work/'.chromix-windows157-crubit-win32-resume.json',json.dumps(m['parent_admission']))
    (work/'fingerprint-diagnostics').mkdir()
    put(work/'fingerprint-diagnostics/source-original.json',json.dumps(m['source_receipt']))
    state=recovery.output_state(src/'out');pgobefore=(src/'chrome/build/pgo_profiles'/profile).read_bytes()
    selected,_=recovery.selected_recipe(view)
    identity=deepcopy(m['source_receipt']['identity']);identity['selection']=selected['selection'];identity['series']={'path':'patches/series','sha256':selected['series_sha256'],'patches':selected['patches']}
    # Tooling Git authentication is isolated here; actual source and full235 replay are not mocked.
    with mock.patch.object(recovery,'source_stack',return_value=(identity,real_stack(),core,tooling)):
        receipt=recovery.migrate(ROOT,view,work,m,'a'*40)
    assert receipt['source']['patch_count']==235 and receipt['changed_source_files']==list(recovery.TARGETS)
    assert recovery.output_state(src/'out')==state
    assert (src/'chrome/build/pgo_profiles'/profile).read_bytes()==pgobefore
    assert not (src/recovery.PENDING).exists() and not (src/recovery.BLOCKER).exists()
    assert (src/'.chromix-patches').read_text().strip()==receipt['preparation_key']!=m['old_preparation_key']
    assert json.loads((work/recovery.RECEIPT).read_text())==receipt
    assert (work/'windows157-compiled-origin'/'.chromix-source-ready').read_text().strip()==markers['.chromix-source-ready']
    assert json.loads((work/'fingerprint-diagnostics/windows157-compiled-source.json').read_text())['patch_count']==235
    with pytest.raises(ValueError,match='already applied'):recovery.migrate(ROOT,view,work,m,'a'*40)


def test_consumer_is_main_only_x64_stage4_to12_and_new_run():
    env=dict(GITHUB_REPOSITORY='xiaozhou26/Chromix',GITHUB_REF='refs/heads/main',GITHUB_EVENT_NAME='workflow_dispatch',
             GITHUB_WORKFLOW_REF=f'xiaozhou26/Chromix/.github/workflows/{recovery.WORKFLOW}.yml@refs/heads/main',
             GITHUB_JOB='build-4',GITHUB_SHA='a'*40,GITHUB_RUN_ID='999',GITHUB_RUN_ATTEMPT='1')
    with mock.patch.dict(os.environ,env):recovery.consumer()
    for key,value in [('GITHUB_JOB','build-1'),('GITHUB_JOB','build-13'),('GITHUB_RUN_ID','44591948738'),
                      ('GITHUB_REF','refs/heads/recovery'),('GITHUB_EVENT_NAME','push'),('GITHUB_RUN_ATTEMPT','0')]:
        with mock.patch.dict(os.environ,dict(env,**{key:value})),pytest.raises(ValueError):recovery.consumer()


def test_recovery_orchestration_pins_and_historical_workflows_are_exact():
    for name,expected_hash in recovery.RECOVERY_CODE.items():
        assert recovery.digest((ROOT/name).read_bytes().replace(b'\r\n',b'\n'))==expected_hash
    for name in ['.github/workflows/build-win-x64-157-crubit-resume.yml',
                 '.github/workflows/build-win-x64-157-fresh.yml',
                 '.github/workflows/build-win-arm64-github.yml']:
        assert subprocess.check_output(['git','-C',str(ROOT),'show',recovery.RECIPE_BASE+':'+name])==(ROOT/name).read_bytes()
    assert recovery.TARGETS==tuple(pin['target'] for pin in recovery.load(ROOT)['additions'])
    assert recovery.load(ROOT)['stage']==3
