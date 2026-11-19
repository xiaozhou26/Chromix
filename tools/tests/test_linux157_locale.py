"""Compiled checkpoint admission, complete private replay and cache preservation."""
from __future__ import annotations

import copy
import difflib
import json
import os
from pathlib import Path
import subprocess
from unittest import mock

import pytest
import yaml

from tools import linux157_locale as locale
from tools import linux157_source as source
from tools import apply_restored_patches as arp

ROOT = source.ROOT


def patch(name, before, after):
    raw = (f'diff --git a/{name} b/{name}\n' + ''.join(difflib.unified_diff(
        before.splitlines(True), after.splitlines(True), fromfile='a/'+name, tofile='b/'+name))).encode()
    data, entries = arp.transform_patch(raw, set(), [])
    return name + '.patch', data, entries


def replay_fixture(tmp_path):
    src = tmp_path / 'src'
    src.mkdir()
    stack = []
    for i in range(225):
        name = f'sources/file{i}.cc'
        p = src / name
        p.parent.mkdir(exist_ok=True)
        p.write_text(f'new{i}\n')
        stack.append(patch(name, f'old{i}\n', f'new{i}\n'))
    target = src / locale.TARGET
    target.parent.mkdir(parents=True)
    target.write_text('SetICUDefaultLocale(locale.getName());\n')
    stack.append(patch(locale.TARGET, 'old locale code\n', target.read_text()))
    stack.append(patch(locale.TARGET, target.read_text(), 'SetICUDefaultLocale(locale);\n'))
    return src, stack


def test_full_226_reverse_forward_then_227_private_roundtrip(tmp_path):
    src, stack = replay_fixture(tmp_path)
    original = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in src.rglob('*') if p.is_file()}
    before, after = locale.private_migration(src, stack, tmp_path)
    assert len(stack) == 227 and len(before) == 226
    assert after[locale.TARGET] == b'SetICUDefaultLocale(locale);\n'
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in original} == original


def test_unknown_old_stack_byte_fails_without_publishing(tmp_path):
    src, stack = replay_fixture(tmp_path)
    (src / 'sources/file8.cc').write_text('unexpected donor edit\n')
    original = (src / locale.TARGET).read_bytes()
    with pytest.raises(ValueError, match='private stack reverse'):
        locale.private_migration(src, stack, tmp_path)
    assert (src / locale.TARGET).read_bytes() == original
    assert not (src / locale.PENDING).exists()


def test_unrelated_addition_cannot_publish(tmp_path):
    src, stack = replay_fixture(tmp_path)
    stack[-1] = patch('sources/file1.cc', 'new1\n', 'modified unrelated\n')
    with pytest.raises(ValueError, match='unrelated'):
        locale.private_migration(src, stack, tmp_path)


def test_compiled_metadata_has_objects_not_old_postgn():
    m = locale.load()
    assert m['run_id'] == 44578881741
    assert m['artifact']['id'] == 14575703787
    assert m['files']['src/out/Chromix/.ninja_log']
    assert m['files']['src/out/Chromix/.ninja_deps']
    assert m['source_final']['patch_count'] == 226
    with pytest.raises(ValueError, match='x64'):
        locale.expected('arm64')


@pytest.mark.parametrize('field', ['run_id','attempt','stage','head_sha','job_id'])
def test_wrong_snapshot_tuple_fails_before_touching_tree(tmp_path, field):
    metadata = locale.expected('x64')
    metadata[field] = 'wrong' if field == 'head_sha' else metadata[field] + 1
    with pytest.raises(ValueError, match='tuple'):
        locale.admit(tmp_path, 'x64', metadata)
    assert not list(tmp_path.iterdir())


def test_actual_run_metadata_is_exact():
    audit = os.environ.get('CHROMIX_LOCALE_AUDIT')
    if not audit:
        pytest.skip('requires exact latest cloud metadata')
    root = Path(audit)
    class Client:
        def get(self, path):return json.loads((root/'run.json').read_text())
        def items(self, path, key):
            return json.loads((root/('jobs.json' if key=='jobs' else 'artifacts.json')).read_text())[key]
    assert locale.validate_metadata(Client(),'x64') == locale.expected('x64')


def test_atomic_publication_never_partially_replaces_file(tmp_path):
    p = tmp_path/'source.cc'
    p.write_bytes(b'old')
    with mock.patch.object(locale.os, 'replace', side_effect=OSError('injected publication failure')):
        with pytest.raises(OSError):locale.atomic_bytes(p,b'new')
    assert p.read_bytes() == b'old'
    assert list(tmp_path.iterdir()) == [p]


def test_partial_migration_blocks_continuation(tmp_path):
    (tmp_path/'src').mkdir()
    (tmp_path/'src'/locale.PENDING).write_text('partial')
    with pytest.raises(ValueError, match='incomplete'):
        locale.verify(tmp_path,'x64')


def test_out_inventory_checks_mtime_and_content_identity_without_writing(tmp_path):
    (tmp_path/'obj').mkdir()
    p=tmp_path/'obj/keep.o';p.write_bytes(b'compiled')
    before=locale.snapshot_outputs(tmp_path)
    assert locale.snapshot_outputs(tmp_path)==before
    os.utime(p,ns=(p.stat().st_atime_ns,p.stat().st_mtime_ns+1000000))
    assert locale.snapshot_outputs(tmp_path)!=before


def test_fixed_workflow_no_arbitrary_donor_and_no_cancellation():
    wf=yaml.safe_load((ROOT/'.github/workflows/build-linux157-x64-locale-resume.yml').read_text())
    events=wf.get('on',wf.get(True))
    assert set(events)=={'workflow_dispatch'}
    assert set(events['workflow_dispatch']['inputs'])=={'compile_jobs'}
    assert wf['concurrency']['cancel-in-progress'] is False
    args=wf['jobs']['resume']['with']
    assert args['arch']=='x64' and args['linux157_locale_recovery'] is True
    assert args['use_upstream_cache'] is False and args['build_profile']=='fast'


def test_generated_restore_only_latest_compiled_helper():
    wf=yaml.safe_load((ROOT/'.github/workflows/build-posix-github.yml').read_text())
    step=next(s for s in wf['jobs']['posix-1']['steps'] if s.get('name')=='Restore latest compiled Linux157 locale checkpoint')
    text=step['run']
    assert 'linux157_locale.py --phase restore --arch x64' in text
    assert 'linux157_postgn.py' not in text and 'prepare-ungoogled' not in text
    assert text.index('linux157_locale.py') < text.index('linux157_source.py') < text.index('verify_linux157_upload.py')
    for i in range(1,9):
        run=next(s for s in wf['jobs'][f'posix-{i}']['steps'] if s.get('id')=='stage')
        assert 'CHROMIX_LINUX157_LOCALE_RECOVERY' in run['env']


def render(script, **changes):
    values={'github.repository':'xiaozhou26/Chromix','github.event_name':'workflow_dispatch',
            'github.ref':'refs/heads/main','inputs.platform':'linux','inputs.arch':'x64',
            'inputs.build_profile':'fast','inputs.use_upstream_cache':'false',
            'inputs.linux157_locale_recovery':'true','inputs.linux157_pre_gn_recovery':'false',
            'inputs.linux157_post_gn_recovery':'false','inputs.resume_migration':'',
            'inputs.resume_run_id':'','inputs.resume_artifact_ids':'','inputs.resume_attempt':'1'}
    values.update(changes)
    for k,v in values.items():script=script.replace('${{ '+k+' }}',v)
    assert '${{' not in script
    return script


@pytest.mark.parametrize('bad',[{'inputs.arch':'arm64'},{'inputs.use_upstream_cache':'true'},
                               {'inputs.linux157_post_gn_recovery':'true'}, {'github.ref':'refs/heads/other'},
                               {'inputs.resume_run_id':'44567777792'},{'inputs.resume_attempt':'2'}])
def test_compiled_workflow_guard_rejects_before_restore(tmp_path,bad):
    wf=yaml.safe_load((ROOT/'.github/workflows/build-posix-github.yml').read_text())
    step=next(s for s in wf['jobs']['posix-1']['steps'] if s.get('name')=='Guard exact Linux157 compiled locale recovery')
    result=subprocess.run(['bash','-c',render(step['run'],**bad)],cwd=tmp_path,
                          env=dict(os.environ,CHROMIUM_VERSION=source.VERSION),capture_output=True)
    assert result.returncode!=0 and not list(tmp_path.iterdir())


def test_generated_restore_migrate_then_two_ninja_stages_preserve_cache(tmp_path):
    from tools.tests.test_linux157_pre_gn_workflow import put
    root=tmp_path.resolve();work=root/'chromix-build';logs=root/'chromix-logs';logs.mkdir()
    calls=root/'calls';binary=root/'bin';binary.mkdir()
    put(binary/'timeout','#!/bin/sh\nshift 5\nexec "$@"\n')
    put(root/'CHROMIUM_LINUX_VERSION',source.VERSION+'\n')
    put(root/'build/posix/ci-stage.sh',(ROOT/'build/posix/ci-stage.sh').read_text())
    put(root/'tools/linux157_locale.py',"""import json,os,sys
from pathlib import Path
a=sys.argv;w=Path(a[a.index('--workdir')+1]);assert not w.exists()
assert a[a.index('--phase')+1]=='restore' and a[a.index('--arch')+1]=='x64'
with open(os.environ['CALL_LOG'],'a') as f:f.write('latest-compiled-restore\\n')
(w/'src/out/Chromix').mkdir(parents=True);(w/'fingerprint-diagnostics').mkdir()
for n,v in [('args.gn','target_cpu=\"x64\"\\nchrome_pgo_phase=0\\n'),('build.ninja','protected graph'),('.ninja_log','# ninja log v7\\n'),('.ninja_deps','protected deps'),('keep.o','compiled object')]:
 (w/'src/out/Chromix'/n).write_text(v)
(w/'src/.chromix-linux157-locale-migration.json').write_text('{\"phase\":\"exact-compiled-locale\",\"count\":227}')
(w/'src/.chromix-source-ready').write_text('reviewed-new-ready')
(w/'fingerprint-diagnostics/source-final.json').write_text('{\"status\":\"verified\",\"patch_count\":227}')
with open(os.environ['CALL_LOG'],'a') as f:f.write('atomic-locale-migration\\n')
""")
    put(root/'tools/linux157_source.py',"""import json,os,sys
from pathlib import Path
a=sys.argv;phase=a[a.index('--phase')+1]
if phase=='checkpoint':
 w=Path(a[a.index('--workdir')+1]);p=json.loads((w/'src/.chromix-linux157-locale-migration.json').read_text())
 assert p=={'phase':'exact-compiled-locale','count':227}
with open(os.environ['CALL_LOG'],'a') as f:f.write(phase+'\\n')
print('{\"status\":\"verified\"}')
""")
    put(root/'tools/verify_linux157_upload.py',"""import json,os,sys
from pathlib import Path
w=Path(sys.argv[sys.argv.index('--workdir')+1]);assert json.loads((w/'fingerprint-diagnostics/source-final.json').read_text())['patch_count']==227
with open(os.environ['CALL_LOG'],'a') as f:f.write('actual-upload-gate\\n')
""")
    put(root/'tools/linux157_build.py',"""import os,sys
from pathlib import Path
w=Path(sys.argv[sys.argv.index('--workdir')+1]);assert (w/'src/out/Chromix/keep.o').read_text()=='compiled object'
with open(os.environ['CALL_LOG'],'a') as f:f.write('continue-ninja\\n')
raise SystemExit(17)
""")
    env=dict(os.environ,RUNNER_TEMP=str(root),GITHUB_WORKSPACE=str(root),CALL_LOG=str(calls),
             PATH=str(binary)+os.pathsep+os.environ['PATH'],CHROMIUM_VERSION=source.VERSION,
             CHROMIX_BUILD_PROFILE='fast',CHROMIX_USE_UPSTREAM_CACHE='0',
             CHROMIX_LINUX157_LOCALE_RECOVERY='1',CHROMIX_LINUX157_PRE_GN_RECOVERY='0',
             CHROMIX_LINUX157_POST_GN_RECOVERY='0')
    wf=yaml.safe_load((ROOT/'.github/workflows/build-posix-github.yml').read_text())
    step=next(s for s in wf['jobs']['posix-1']['steps'] if s.get('name')=='Restore latest compiled Linux157 locale checkpoint')
    result=subprocess.run(['bash','-c',render(step['run'])],cwd=root,env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    protected={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in (work/'src/out/Chromix').iterdir()}
    for stage in (1,2):
        result=subprocess.run(['bash',str(root/'build/posix/ci-stage.sh'),'--platform','linux','--arch','x64',
                               '--stage-index',str(stage),'--workdir',str(work)],cwd=root,env=env,capture_output=True,text=True)
        assert result.returncode==1 and 'build script failed' in result.stderr
    text=calls.read_text();assert text.count('latest-compiled-restore')==1 and text.count('continue-ninja')==2
    assert text.index('atomic-locale-migration')<text.index('actual-upload-gate')<text.index('continue-ninja')
    for p,state in protected.items():assert (p.read_bytes(),p.stat().st_mtime_ns)==state


def test_missing_reviewed0236_blocks_before_download(tmp_path):
    m=locale.load();old=m['source_receipt']['chromix_selection']
    with mock.patch.object(locale.patch_selection,'select',return_value=(old,[('old',b'')]*226)), \
            mock.patch.object(locale,'validate_metadata') as metadata:
        with pytest.raises(ValueError,match='unavailable'):
            locale.restore(tmp_path/'work','x64')
        metadata.assert_not_called()
    assert not (tmp_path/'work').exists()


def test_multi_source_scope_cannot_be_smuggled_as_one0236(tmp_path):
    m=locale.load();selected=copy.deepcopy(m['source_receipt']['chromix_selection'])
    one=patch(locale.TARGET,'old\n','new\n')[1]
    two=patch('base/i18n/icubridge/default_icu_locale.h','old\n','new\n')[1]
    name='patches/chromium157-additions/0236-review.patch';raw=one+two
    selected['patches'].append({'path':name,'sha256':source.digest(raw)})
    with mock.patch.object(locale.patch_selection,'select',return_value=(selected,[('old',b'')]*226+[(name,raw)])), \
            mock.patch.object(locale,'validate_metadata') as metadata:
        with pytest.raises(ValueError,match='only timezone'):
            locale.restore(tmp_path/'work','x64')
        metadata.assert_not_called()


def test_interrupted_publication_keeps_block_marker_and_old_or_new_whole_file(tmp_path):
    src,stack=replay_fixture(tmp_path)
    _,new=locale.private_migration(src,stack,tmp_path)
    target=src/locale.TARGET;old=target.read_bytes()
    (src/locale.PENDING).write_text('transaction in progress')
    with mock.patch.object(locale.os,'replace',side_effect=OSError('interrupted before replace')):
        with pytest.raises(OSError):locale.atomic_bytes(target,new[locale.TARGET])
    assert target.read_bytes()==old
    with pytest.raises(ValueError,match='incomplete'):locale.verify(tmp_path,'x64')
    locale.atomic_bytes(target,new[locale.TARGET])
    assert target.read_bytes()==new[locale.TARGET]
    with pytest.raises(ValueError,match='incomplete'):locale.verify(tmp_path,'x64')
