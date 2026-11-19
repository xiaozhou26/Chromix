"""Execute fixed pre-GN workflow restore/admission and same-recipe continuation."""
from __future__ import annotations

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

from tools import linux157_checkpoint as checkpoint
from tools import linux157_source as source
from tools import linux157_build as build

ROOT = Path(__file__).resolve().parents[2]


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


def workflow():
    return yaml.safe_load((ROOT / '.github/workflows/build-posix-github.yml').read_text())


def render(text, arch='x64', **overrides):
    values = {'github.repository':'xiaozhou26/Chromix', 'github.event_name':'workflow_dispatch',
              'github.ref':'refs/heads/main', 'inputs.platform':'linux', 'inputs.build_profile':'fast',
              'inputs.use_upstream_cache':'false', 'inputs.resume_migration':'', 'inputs.resume_run_id':'',
              'inputs.resume_artifact_ids':'', 'inputs.resume_attempt':'1', 'inputs.arch':arch,
              'inputs.linux157_pre_gn_recovery':'true', 'inputs.linux157_locale_recovery':'false', "inputs['max-stages']":'8'}
    values.update(overrides)
    for key, value in values.items(): text = text.replace('${{ '+key+' }}', value)
    assert '${{' not in text
    return text


@pytest.mark.parametrize('arch,run,artifact,runner', [
    ('x64',44547858334,14542955675,'ubuntu-22.04'),
    ('arm64',44547869671,14544060048,'ubuntu-24.04')])
def test_dedicated_entries_have_no_arbitrary_donor_controls(arch,run,artifact,runner):
    value=yaml.safe_load((ROOT/f'.github/workflows/build-linux157-{arch}-pre-gn-resume.yml').read_text())
    events=value.get('on',value.get(True))
    assert set(events)=={'workflow_dispatch'}
    assert set(events['workflow_dispatch']['inputs'])=={'compile_jobs'}
    inputs=value['jobs']['resume']['with']
    assert inputs['linux157_pre_gn_recovery'] is True and inputs['use_upstream_cache'] is False
    assert inputs['arch']==arch and inputs['runner']==runner and inputs['build_profile']=='fast'
    donor=checkpoint.expected(arch)
    assert donor['run_id']==run and donor['artifacts'][0]['id']==artifact
    assert donor['stage']==donor['attempt']==1


@pytest.mark.parametrize('bad', [{'inputs.platform':'macos'}, {'inputs.use_upstream_cache':'true'},
                                 {'inputs.build_profile':'release'}, {'inputs.resume_run_id':'44541471654'},
                                 {'inputs.resume_migration':'linux156-ts'}, {'inputs.resume_attempt':'2'},
                                 {'github.ref':'refs/heads/other'}, {'inputs.arch':'riscv64'}])
def test_generated_guard_rejects_invalid_request_without_tools(tmp_path,bad):
    steps=workflow()['jobs']['posix-1']['steps']
    guard=next(s for s in steps if s.get('name')=='Guard exact Linux157 pre-GN recovery request')
    env=dict(os.environ,CHROMIUM_VERSION=source.VERSION)
    result=subprocess.run(['bash','-c',render(guard['run'],**bad)],cwd=tmp_path,env=env,capture_output=True)
    assert result.returncode!=0
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('arch',['x64','arm64'])
@pytest.mark.parametrize('failure',['none','download','admit','sourceproof','existing-work','wrong-proof','wrong-recipe','no-optin'])
def test_execute_generated_restore_admit_and_stage_chain(tmp_path,arch,failure):
    root=tmp_path.resolve();work=root/'chromix-build';logs=root/'chromix-logs';logs.mkdir()
    calls=root/'calls';binaries=root/'bin';binaries.mkdir()
    put(binaries/'uname','#!/bin/sh\necho x86_64\n')
    put(binaries/'timeout','#!/bin/sh\nshift 5\nexec "$@"\n')
    put(root/'CHROMIUM_LINUX_VERSION',source.VERSION+'\n')
    put(root/'build/posix/ci-stage.sh',(ROOT/'build/posix/ci-stage.sh').read_text())
    put(root/'build/prepare-ungoogled.sh','#!/bin/sh\necho forbidden-fresh >> "$CALL_LOG"\nexit 99\n')
    put(root/'tools/linux157_checkpoint.py', '''import json,os,sys
from pathlib import Path
a=sys.argv;work=Path(a[a.index('--workdir')+1]);arch=a[a.index('--arch')+1]
assert a[a.index('--phase')+1]=='restore'
assert not work.exists()
def log(s):
 with open(os.environ['CALL_LOG'],'a') as f:f.write(s+'\\n')
log('metadata:'+arch)
if os.environ['FAIL']=='download':raise SystemExit(21)
(work/'src/out/Chromix').mkdir(parents=True)
(work/'src/source.cc').write_text('unchanged source')
(work/'src/out/Chromix/keep.o').write_bytes(b'keep object')
(work/'src/out/Chromix/args.gn').write_text('target_cpu="'+arch+'"\\nchrome_pgo_phase=0\\nrust_prebuilt_stdlib = false\\nuse_chromium_rust_toolchain = true\\nv8_use_metagen_instance_types=false\\n')
(work/'linux157-checkpoint-download.json').write_text(json.dumps({'verified':True,'arch':arch}))
log('download-restore:'+arch)
if os.environ['FAIL']=='admit':raise SystemExit(22)
(work/'linux157-pre-gn-origin').mkdir()
(work/'linux157-pre-gn-origin/args.gn').write_bytes((work/'src/out/Chromix/args.gn').read_bytes())
proof={'arch':arch,'phase':'exact-stage1-pre-gn','recipe':'new-reviewed-recipe'}
(work/'src/.chromix-linux157-gn-migration.json').write_text(json.dumps(proof))
(work/'src/.chromix-source-ready').write_text('new-ready')
log('admit:'+arch)
print(json.dumps(proof))
def verify_migrated(work,arch):
 assert (work/'src/.chromix-linux157-gn-migration.json').is_file()
def prepare_args(work,arch):
 p=work/'src/out/Chromix/args.gn';s=p.read_text()
 for line in ('rust_prebuilt_stdlib = false\\n','use_chromium_rust_toolchain = true\\n','v8_use_metagen_instance_types=false\\n'):s=s.replace(line,'')
 p.write_text(s)
''')
    # The shell imports the frozen helper's API after its restore CLI finishes.
    helper=root/'tools/linux157_checkpoint.py'
    text=helper.read_text();split=text.index('def verify_migrated')
    cli=text[:split];api=text[split:]
    helper.write_text('from pathlib import Path\n'+api+'\nif __name__=="__main__":\n'+''.join(' '+l+'\n' for l in cli.splitlines()))
    put(root/'tools/verify_patch_stack.py', '''import os,sys
from pathlib import Path
if os.environ['FAIL']=='sourceproof':raise SystemExit(24)
p=Path(sys.argv[sys.argv.index('--output')+1]);p.parent.mkdir(parents=True,exist_ok=True);p.write_text('{"status":"verified"}')
with open(os.environ['CALL_LOG'],'a') as f:f.write('admission-source-proof\\n')
''')
    put(root/'tools/linux157_source.py', '''import json,os,sys
from pathlib import Path
a=sys.argv;phase=a[a.index('--phase')+1];arch=a[a.index('--arch')+1]
with open(os.environ['CALL_LOG'],'a') as f:f.write(phase+':'+arch+'\\n')
if phase=='checkpoint':
 w=Path(a[a.index('--workdir')+1]);p=json.loads((w/'src/.chromix-linux157-gn-migration.json').read_text())
 assert p=={'arch':arch,'phase':'exact-stage1-pre-gn','recipe':'new-reviewed-recipe'}
 assert (w/'src/source.cc').read_text()=='unchanged source'
 assert (w/'src/out/Chromix/keep.o').read_bytes()==b'keep object'
print('{"status":"verified"}')
''')
    put(root/'tools/linux157_build.py', '''import os,sys
from pathlib import Path
a=sys.argv;w=Path(a[a.index('--workdir')+1]);arch=a[a.index('--arch')+1]
p=w/'src/out/Chromix/args.gn';s=p.read_text()
for line in ('rust_prebuilt_stdlib = false\\n','use_chromium_rust_toolchain = true\\n','v8_use_metagen_instance_types=false\\n'):s=s.replace(line,'')
p.write_text(s)
assert (w/'src/out/Chromix/keep.o').read_bytes()==b'keep object'
with open(os.environ['CALL_LOG'],'a') as f:f.write('source-proof:'+arch+'\\nargs-repair:'+arch+'\\ncontinue-build:'+arch+'\\n')
raise SystemExit(17)
''')
    env=dict(os.environ,RUNNER_TEMP=str(root),GITHUB_WORKSPACE=str(root),CALL_LOG=str(calls),FAIL=failure,GH_TOKEN='fixture',
             PATH=str(binaries)+os.pathsep+os.environ['PATH'],CHROMIUM_VERSION=source.VERSION,
             CHROMIX_BUILD_PROFILE='fast',CHROMIX_USE_UPSTREAM_CACHE='0',CHROMIX_LINUX157_PRE_GN_RECOVERY='1')
    steps=workflow()['jobs']['posix-1']['steps']
    restore=next(s for s in steps if s.get('name')=='Restore exact Linux157 prepared stage1 checkpoint')
    if failure=='existing-work':work.mkdir()
    result=subprocess.run(['bash','-c',render(restore['run'],arch)],cwd=root,env=env,capture_output=True,text=True)
    if failure in ('download','admit','sourceproof','existing-work'):
        assert result.returncode!=0
        assert not calls.exists() or 'continue-build' not in calls.read_text()
        return
    assert result.returncode==0,result.stderr
    original=(work/'src/source.cc').read_bytes()
    if failure in ('wrong-proof','wrong-recipe'):
        p=work/'src/.chromix-linux157-gn-migration.json';proof=json.loads(p.read_text())
        proof['phase' if failure=='wrong-proof' else 'recipe']='unknown';p.write_text(json.dumps(proof))
    if failure=='no-optin':env['CHROMIX_LINUX157_PRE_GN_RECOVERY']='0'
    for stage in (1,2):
        result=subprocess.run(['bash',str(root/'build/posix/ci-stage.sh'),'--platform','linux','--arch',arch,
                               '--stage-index',str(stage),'--workdir',str(work)],env=env,cwd=root,capture_output=True,text=True)
        assert result.returncode==1
        if failure!='none':
            assert 'continue-build' not in calls.read_text()
            return
        assert 'build script failed' in result.stderr
    assert calls.read_text().count('metadata:')==1
    assert calls.read_text().count('admit:')==1
    assert calls.read_text().count('continue-build:')==2
    assert 'forbidden-fresh' not in calls.read_text()
    assert (work/'src/source.cc').read_bytes()==original
    assert (work/'src/out/Chromix/args.gn').read_text()==f'target_cpu="{arch}"\nchrome_pgo_phase=0\n'


def test_current_builder_checks_postgn_proof_without_repeating_args_repair(tmp_path):
    work=tmp_path;src=work/'src';src.mkdir();(src/'.chromix-domain-substituted').touch()
    order=[]
    def run(args,**kwargs):
        if 'tools/verify_patch_stack.py' in ' '.join(args):
            order.append('source-proof')
            Path(args[args.index('--output')+1]).write_text('{}')
        return subprocess.CompletedProcess(args,0)
    with mock.patch.object(build.toolchain.platform,'system',return_value='Linux'), \
         mock.patch.object(build.toolchain.platform,'machine',return_value='x86_64'), \
         mock.patch.dict(os.environ,{'CHROMIX_USE_UPSTREAM_CACHE':'0','CHROMIX_UPSTREAM_CACHE_DIR':''}), \
         mock.patch.object(build.source,'prepare',side_effect=lambda *a:order.append('verify-migrated-source')), \
         mock.patch.object(build.toolchain,'prepare',side_effect=lambda *a:order.append('verified-tools')), \
         mock.patch.object(build.source,'get_platform'), \
         mock.patch.object(build.source,'verify_receipt',return_value={'recipe':{}}), \
         mock.patch.object(build,'prepare_host_go',return_value={}), \
         mock.patch.object(build.shutil,'which',return_value=sys.executable), \
         mock.patch.object(build.subprocess,'run',side_effect=run), \
         mock.patch.object(build.locale_recovery,'verify',side_effect=lambda *a:order.append('read-postgn-proof')), \
         mock.patch.object(build.host_gn,'prepare',side_effect=ValueError('stop-before-GN')):
        with pytest.raises(ValueError,match='stop-before-GN'):build.build(work,'arm64')
    assert order==['verify-migrated-source','verified-tools','source-proof','read-postgn-proof']


def test_stage_layout_and_native_gate_unchanged():
    data=workflow()
    original=yaml.safe_load(subprocess.check_output(['git','-C',str(ROOT),'show','HEAD:.github/workflows/build-posix-github.yml']))
    assert data['jobs']['verify-linux-arm64']==original['jobs']['verify-linux-arm64']
    for stage in range(1,9):
        steps=data['jobs'][f'posix-{stage}']['steps']
        restores=[s for s in steps if s.get('name')=='Restore exact Linux157 prepared stage1 checkpoint']
        assert len(restores)==(1 if stage==1 else 0)
        if restores:
            assert steps.index(restores[0])<next(i for i,s in enumerate(steps) if s.get('name')=='Restore pinned source downloads')
        assert next(s for s in steps if s.get('id')=='stage')['env']['CHROMIX_LINUX157_PRE_GN_RECOVERY'].endswith("'1' || '0' }}")
        for a,b in zip([s for s in steps if s.get('name','').startswith('Upload tree part')],
                       [s for s in original['jobs'][f'posix-{stage}']['steps'] if s.get('name','').startswith('Upload tree part')]):
            assert a==b
