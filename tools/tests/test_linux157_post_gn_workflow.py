"""Execute latest post-GN restore routing without replacing source or GN state."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest
import yaml

from tools import linux157_postgn as postgn
from tools import linux157_source as source
from tools import linux157_build as build
from tools.tests.test_linux157_pre_gn_workflow import put, workflow, render as old_render

ROOT = Path(__file__).resolve().parents[2]


def render(script, arch='x64', **changes):
    return old_render(script, arch, **{'inputs.linux157_post_gn_recovery':'true',
                                     'inputs.linux157_pre_gn_recovery':'false', **changes})


@pytest.mark.parametrize('arch,run,artifact,runner', [
    ('x64',44567777792,14554252116,'ubuntu-22.04'),
    ('arm64',44567785761,14554286193,'ubuntu-24.04')])
def test_fixed_latest_entry(arch,run,artifact,runner):
    data=yaml.safe_load((ROOT/f'.github/workflows/build-linux157-{arch}-post-gn-resume.yml').read_text())
    events=data.get('on',data.get(True));assert set(events)=={'workflow_dispatch'}
    assert set(events['workflow_dispatch']['inputs'])=={'compile_jobs'}
    args=data['jobs']['resume']['with']
    assert args['linux157_post_gn_recovery'] is True and 'linux157_pre_gn_recovery' not in args
    assert args['runner']==runner and args['arch']==arch and args['use_upstream_cache'] is False
    assert postgn.expected(arch)['run_id']==run
    assert postgn.expected(arch)['artifacts'][0]['id']==artifact


@pytest.mark.parametrize('bad',[{'inputs.linux157_pre_gn_recovery':'true'}, {'inputs.use_upstream_cache':'true'},
                               {'inputs.platform':'macos'}, {'inputs.build_profile':'release'},
                               {'inputs.resume_run_id':'44547858334'}, {'github.ref':'refs/heads/other'},
                               {'inputs.resume_attempt':'2'}])
def test_guard_rejects_old_or_mixed_route(tmp_path,bad):
    guard=next(s for s in workflow()['jobs']['posix-1']['steps'] if s.get('name')=='Guard exact Linux157 post-GN recovery request')
    result=subprocess.run(['bash','-c',render(guard['run'],**bad)],cwd=tmp_path,capture_output=True,
                          env=dict(os.environ,CHROMIUM_VERSION=source.VERSION))
    assert result.returncode!=0 and not list(tmp_path.iterdir())


@pytest.mark.parametrize('arch',['x64','arm64'])
@pytest.mark.parametrize('failure',['none','metadata','admit','sourceproof','upload','existing','phase','recipe','no-optin','both'])
def test_real_generated_restore_then_two_stages(tmp_path,arch,failure):
    root=tmp_path.resolve();work=root/'chromix-build';(root/'chromix-logs').mkdir()
    calls=root/'calls';binary=root/'bin';binary.mkdir()
    put(binary/'timeout','#!/bin/sh\nshift 5\nexec "$@"\n')
    put(root/'CHROMIUM_LINUX_VERSION',source.VERSION+'\n')
    put(root/'build/posix/ci-stage.sh',(ROOT/'build/posix/ci-stage.sh').read_text())
    put(root/'build/prepare-ungoogled.sh','#!/bin/sh\necho forbidden-fresh >> "$CALL_LOG"\nexit 99\n')
    put(root/'tools/linux157_checkpoint.py',"raise SystemExit('forbidden old donor')\n")
    put(root/'tools/linux157_postgn.py','''import os,sys,json
from pathlib import Path
a=sys.argv;w=Path(a[a.index('--workdir')+1]);arch=a[a.index('--arch')+1]
assert a[a.index('--phase')+1]=='restore' and not w.exists()
def log(s):
 with open(os.environ['CALL_LOG'],'a') as f:f.write(s+'\\n')
log('metadata:'+arch)
if os.environ['FAIL']=='metadata':raise SystemExit(21)
(w/'src/out/Chromix').mkdir(parents=True)
(w/'src/source.cc').write_text('protected source')
for n,v in [('args.gn','target_cpu="'+arch+'"\\nchrome_pgo_phase=0\\n'),('build.ninja','protected graph'),('keep.o','protected object')]:
 (w/'src/out/Chromix'/n).write_text(v)
(w/'linux157-postgn-download.json').write_text('{"verified":true}')
if os.environ['FAIL']=='admit':raise SystemExit(22)
proof={'phase':'exact-post-GN-libclang','arch':arch,'recipe':'current-reviewed-recipe'}
(w/'src/.chromix-linux157-libclang-migration.json').write_text(json.dumps(proof))
(w/'src/.chromix-source-ready').write_text('current-ready')
log('admit:'+arch)
print(json.dumps(proof))
''')
    put(root/'tools/linux157_source.py','''import os,sys,json
from pathlib import Path
a=sys.argv;phase=a[a.index('--phase')+1];arch=a[a.index('--arch')+1]
with open(os.environ['CALL_LOG'],'a') as f:f.write(phase+':'+arch+'\\n')
assert phase in ('route','checkpoint')
if phase=='checkpoint':
 w=Path(a[a.index('--workdir')+1]);p=json.loads((w/'src/.chromix-linux157-libclang-migration.json').read_text())
 assert p=={'phase':'exact-post-GN-libclang','arch':arch,'recipe':'current-reviewed-recipe'}
print('{"status":"verified"}')
''')
    put(root/'tools/verify_patch_stack.py','''import os,sys,json
from pathlib import Path
if os.environ['FAIL']=='sourceproof':raise SystemExit(23)
p=Path(sys.argv[sys.argv.index('--output')+1]);p.parent.mkdir(parents=True,exist_ok=True)
p.write_text(json.dumps({'status':'verified','recipe':'current-reviewed-recipe'}))
with open(os.environ['CALL_LOG'],'a') as f:f.write('sourceproof\\n')
''')
    put(root/'tools/verify_linux157_upload.py','''import os,sys,json
from pathlib import Path
w=Path(sys.argv[sys.argv.index('--workdir')+1])
assert json.loads((w/'fingerprint-diagnostics/source-final.json').read_text())['recipe']=='current-reviewed-recipe'
with open(os.environ['CALL_LOG'],'a') as f:f.write('upload-admission\\n')
if os.environ['FAIL']=='upload':raise SystemExit(24)
print('{"status":"verified","runtime_verified":false}')
''')
    put(root/'tools/linux157_build.py','''import os,sys
from pathlib import Path
w=Path(sys.argv[sys.argv.index('--workdir')+1]);assert (w/'src/out/Chromix/build.ninja').read_text()=='protected graph'
with open(os.environ['CALL_LOG'],'a') as f:f.write('tool-only-build\\n')
raise SystemExit(17)
''')
    env=dict(os.environ,RUNNER_TEMP=str(root),GITHUB_WORKSPACE=str(root),CALL_LOG=str(calls),FAIL=failure,
             PATH=str(binary)+os.pathsep+os.environ['PATH'],CHROMIUM_VERSION=source.VERSION,
             CHROMIX_BUILD_PROFILE='fast',CHROMIX_USE_UPSTREAM_CACHE='0',
             CHROMIX_LINUX157_POST_GN_RECOVERY='1',CHROMIX_LINUX157_PRE_GN_RECOVERY='0')
    restore=next(s for s in workflow()['jobs']['posix-1']['steps'] if s.get('name')=='Restore exact Linux157 latest post-GN checkpoint')
    if failure=='existing':work.mkdir()
    result=subprocess.run(['bash','-c',render(restore['run'],arch)],cwd=root,env=env,capture_output=True,text=True)
    if failure in ('metadata','admit','sourceproof','upload','existing'):
        assert result.returncode!=0
        assert not calls.exists() or 'tool-only-build' not in calls.read_text()
        return
    assert result.returncode==0,result.stderr
    protected={str(p.relative_to(work)):(p.read_bytes(),p.stat().st_mtime_ns) for p in (work/'src').rglob('*') if p.is_file()}
    if failure in ('phase','recipe'):
        p=work/'src/.chromix-linux157-libclang-migration.json';data=json.loads(p.read_text());data[failure]='wrong';p.write_text(json.dumps(data))
    if failure=='no-optin':env['CHROMIX_LINUX157_POST_GN_RECOVERY']='0'
    if failure=='both':env['CHROMIX_LINUX157_PRE_GN_RECOVERY']='1'
    for stage in (1,2):
        result=subprocess.run(['bash',str(root/'build/posix/ci-stage.sh'),'--platform','linux','--arch',arch,
                               '--stage-index',str(stage),'--workdir',str(work)],cwd=root,env=env,capture_output=True,text=True)
        assert result.returncode==1
        if failure!='none':
            assert 'tool-only-build' not in calls.read_text();return
        assert 'build script failed' in result.stderr
    assert calls.read_text().count('metadata:')==1 and calls.read_text().count('admit:')==1
    assert calls.read_text().count('tool-only-build')==2 and 'forbidden' not in calls.read_text()
    assert calls.read_text().index('sourceproof')<calls.read_text().index('upload-admission')<calls.read_text().index('tool-only-build')
    for n,state in protected.items():assert ((work/n).read_bytes(),(work/n).stat().st_mtime_ns)==state


def test_real_builder_installs_libclang_before_ninja_without_rewriting_args(tmp_path):
    src=tmp_path/'src';out=src/'out/Chromix';out.mkdir(parents=True)
    (src/'.chromix-domain-substituted').touch()
    (out/'args.gn').write_text('protected args')
    (out/'build.ninja').write_text('protected graph')
    order=[]
    def run(args,**kwargs):
        if 'tools/verify_patch_stack.py' in ' '.join(args):
            order.append('sourceproof');Path(args[args.index('--output')+1]).write_text('{}')
        elif args[0]==str(out/'gn'):order.append('gn-gen')
        else:raise AssertionError(args)
        return subprocess.CompletedProcess(args,0)
    with mock.patch.object(build.toolchain.platform,'system',return_value='Linux'), \
         mock.patch.object(build.toolchain.platform,'machine',return_value='x86_64'), \
         mock.patch.dict(os.environ,{'CHROMIX_USE_UPSTREAM_CACHE':'0','CHROMIX_UPSTREAM_CACHE_DIR':''}), \
         mock.patch.object(build.source,'prepare',side_effect=lambda *a:order.append('read-source-proof')), \
         mock.patch.object(build.toolchain,'prepare',return_value={}), \
         mock.patch.object(build.source,'get_platform'), \
         mock.patch.object(build.source,'verify_receipt',return_value={'recipe':{}}), \
         mock.patch.object(build,'prepare_host_go',return_value={}), \
         mock.patch.object(build.shutil,'which',return_value=sys.executable), \
         mock.patch.object(build.subprocess,'run',side_effect=run), \
         mock.patch.object(build.locale_recovery,'verify',side_effect=lambda *a:order.append('read-postgn-proof')), \
         mock.patch.object(build.host_gn,'prepare',return_value={}), \
         mock.patch.object(build.libclang,'prepare',side_effect=ValueError('libclang-before-ninja')) as install:
        with pytest.raises(ValueError,match='libclang-before-ninja'):build.build(tmp_path,'arm64')
        install.assert_called_once_with(src,source.ROOT)
    assert order==['read-source-proof','sourceproof','read-postgn-proof']
    assert (out/'args.gn').read_text()=='protected args'
    assert (out/'build.ninja').read_text()=='protected graph'


def test_actual_upload_validator_accepts_only_reproved_current_selection(tmp_path, monkeypatch):
    from tools import verify_linux157_upload as upload
    from tools.tests import test_linux157_upload as fixtures
    # Use the actual source-final live-byte validator and native Ninja version probe.
    root, work = fixtures.tree.__wrapped__(tmp_path, monkeypatch)
    fixtures.run_ninja(work)
    inputs = {'series_sha256':'a'*64, 'patches':[{'path':'fixture.patch','sha256':'b'*64}],
              'selection':{'downstream':{'recipe':{'sha256':'new-reviewed-recipe'}}}}
    monkeypatch.setattr(upload.verify_patch_stack,'load_stack',lambda *a,**k:(
        inputs,[('fixture.patch',b'',[('value.cc','modify',None)])]))
    with pytest.raises(ValueError,match='different patch stack'):upload.validate(work,'x64')
    path=work/'fingerprint-diagnostics/source-final.json';report=json.loads(path.read_text())
    report['identity']=inputs;path.write_text(json.dumps(report))
    accepted=upload.validate(work,'x64')
    assert accepted['source_final']['sha256']==upload.sha(path)
    assert accepted['runtime_verified'] is False


def test_existing_routes_and_native_upload_steps_unchanged():
    current=workflow();old=yaml.safe_load(subprocess.check_output(['git','-C',str(ROOT),'show','HEAD:.github/workflows/build-posix-github.yml']))
    assert current['jobs']['verify-linux-arm64']==old['jobs']['verify-linux-arm64']
    for stage in range(1,9):
        steps=current['jobs'][f'posix-{stage}']['steps'];previous=old['jobs'][f'posix-{stage}']['steps']
        for name in ('Restore exact Linux157 prepared stage1 checkpoint','Guard exact Linux157 pre-GN recovery request','Verify handoff snapshot'):
            a=[s for s in steps if s.get('name')==name];b=[s for s in previous if s.get('name')==name]
            assert a==b
        assert len([s for s in steps if s.get('name')=='Restore exact Linux157 latest post-GN checkpoint'])==(1 if stage==1 else 0)
    for arch in ('x64','arm64'):
        p=f'.github/workflows/build-linux157-{arch}-pre-gn-resume.yml'
        assert (ROOT/p).read_bytes()==subprocess.check_output(['git','-C',str(ROOT),'show','HEAD:'+p])
