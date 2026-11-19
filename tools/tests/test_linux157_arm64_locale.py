"""Exact ARM stage2 donor continuation at logical stage3 without x64 drift."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
from unittest import mock

import pytest
import yaml

from tools import linux157_locale as locale
from tools import gen_posix_workflow as base
from tools import gen_linux157_arm64_locale_workflow as arm
from tools.tests.test_linux157_pre_gn_workflow import put

ROOT = locale.source.ROOT


def test_fixed_arm_stage2_and_x64_stage1_remain_distinct():
    x64 = locale.expected('x64')
    current = locale.expected('arm64')
    assert (x64['run_id'],x64['stage'],x64['artifacts'][0]['id']) == (44578881741,1,14575703787)
    assert (current['run_id'],current['stage'],current['attempt'],current['job_id']) == (44578888695,2,1,135914681758)
    assert current['artifacts'][0]['id'] == 14583218428
    assert current['artifacts'][0]['size_in_bytes'] == 4091870997
    assert current['artifacts'][0]['digest'] == 'sha256:135d45554786f0472d6c3783fe0eaccda780cec7ee3258fc7c67eeeda276cfda'
    assert current['pattern'] == 'chromix-linux-arm64-tree-s2-attempt-1-part*'
    manifest = locale.load(ROOT,'arm64')
    assert manifest['continuation_stage']==3
    assert manifest['files']['src/out/Chromix/.ninja_log']
    assert manifest['files']['src/out/Chromix/.ninja_deps']
    assert manifest['migration'] == locale.load()['migration']


@pytest.mark.parametrize('field',['run_id','stage','attempt','job_id','head_sha','arch'])
def test_wrong_arm_tuple_never_touches_worktree(tmp_path,field):
    metadata=locale.expected('arm64')
    metadata[field]='wrong' if field in ('head_sha','arch') else metadata[field]+1
    with pytest.raises(ValueError,match='tuple'):
        locale.admit(tmp_path,'arm64',metadata)
    assert not list(tmp_path.iterdir())


def real_client(attack=None):
    audit=os.environ.get('CHROMIX_ARM_LOCALE_AUDIT')
    if not audit:pytest.skip('requires latest ARM stage2 GitHub metadata')
    root=Path(audit);run=json.loads((root/'run.json').read_text());jobs=json.loads((root/'jobs.json').read_text())['jobs'];artifacts=json.loads((root/'artifacts.json').read_text())['artifacts']
    job=next(j for j in jobs if j['id']==135914681758)
    artifact=next(a for a in artifacts if a['id']==14583218428)
    if attack=='attempt':run['run_attempt']=2
    elif attack=='head':run['head_sha']='0'*40
    elif attack=='job':job['id']=135914681759
    elif attack=='sha':artifact['digest']='sha256:'+'0'*64
    elif attack=='stage':artifact['name']=artifact['name'].replace('-s2-','-s1-')
    elif attack=='arch':artifact['name']=artifact['name'].replace('-arm64-','-x64-')
    elif attack=='step':
        next(s for s in job['steps'] if s['name']=='Run stage 2')['name']='Run stage 1'
    class Client:
        def get(self,path):return run
        def items(self,path,key):return jobs if key=='jobs' else artifacts
    return Client()


def test_real_arm_metadata_accepts_only_stage2():
    assert locale.validate_metadata(real_client(),'arm64')==locale.expected('arm64')


@pytest.mark.parametrize('attack',['attempt','head','job','sha','stage','arch','step'])
def test_real_metadata_mutation_rejected(attack):
    with pytest.raises(ValueError):locale.validate_metadata(real_client(attack),'arm64')


def test_separate_generator_retains_default_workflow_and_exact_stages():
    default=base.HEADER+'\n'.join(base.job(s) for s in range(1,9))+'\n'+base.NATIVE_LINUX_ARM64
    assert default==(ROOT/'.github/workflows/build-posix-github.yml').read_text()
    rendered=arm.render();assert rendered==(ROOT/arm.OUT).read_text()
    data=yaml.safe_load(rendered);jobs=data['jobs']
    assert set(jobs)=={*(f'posix-{i}' for i in range(3,9)),'verify-linux-arm64'}
    assert 'needs' not in jobs['posix-3']
    assert jobs['posix-4']['needs']=='posix-3'
    run=next(s for s in jobs['posix-3']['steps'] if s.get('id')=='stage')
    assert '--stage-index 3' in run['run'] and '--from-snapshot' not in run['run']
    assert run['env']['CHROMIX_LINUX157_ARM64_STAGE2_RECOVERY']=='1'
    restore=next(s for s in jobs['posix-3']['steps'] if s.get('name')=='Restore exact ARM stage2 compiled locale checkpoint')
    assert '--arch arm64' in restore['run'] and '--arch x64' not in restore['run']
    assert not any(s.get('name')=='Download tree from previous stage' for s in jobs['posix-3']['steps'])
    for stage in range(3,9):
        steps=jobs[f'posix-{stage}']['steps']
        assert any(s.get('name')=='Preserve latest failed Linux source/output checkpoint' for s in steps)
        upload=next(s for s in steps if s.get('name')=='Upload tree part 1')
        assert f'-tree-s{stage}-attempt-' in upload['with']['name']
    old=yaml.safe_load(base.NATIVE_LINUX_ARM64)['verify-linux-arm64'];new=jobs['verify-linux-arm64']
    assert new['needs']==[f'posix-{i}' for i in range(3,9)]
    assert {k:v for k,v in new.items() if k!='needs'}=={k:v for k,v in old.items() if k!='needs'}


def test_fixed_entry_cannot_cancel_x64_or_select_stage1():
    data=yaml.safe_load((ROOT/'.github/workflows/build-linux157-arm64-locale-resume.yml').read_text())
    events=data.get('on',data.get(True));assert set(events)=={'workflow_dispatch'}
    assert set(events['workflow_dispatch']['inputs'])=={'compile_jobs'}
    assert data['concurrency']['cancel-in-progress'] is False
    assert 'arm64-locale-resume' in data['concurrency']['group']
    assert data['jobs']['resume']['uses'].endswith('build-linux157-arm64-locale-stages.yml')
    inputs=data['jobs']['resume']['with'];assert inputs['arch']=='arm64' and inputs['runner']=='ubuntu-24.04'


@pytest.mark.parametrize('stage,enabled,expect_build',[(1,'1',False),(2,'1',False),(3,'0',False),(3,'1',True),(4,'1',True)])
def test_ci_arm_requires_stage2_optin_and_logical_stage3(tmp_path,stage,enabled,expect_build):
    work=tmp_path/'work';(work/'src/out/Chromix').mkdir(parents=True)
    (work/'src/out/Chromix/keep.o').write_bytes(b'compiled-arm64')
    put(tmp_path/'CHROMIUM_LINUX_VERSION','157.0.8103.78\n')
    put(tmp_path/'build/posix/ci-stage.sh',(ROOT/'build/posix/ci-stage.sh').read_text())
    put(tmp_path/'tools/linux157_source.py',"print('{}')\n")
    put(tmp_path/'tools/linux157_build.py',"from pathlib import Path\nimport os\nPath(os.environ['CALL_LOG']).write_text('continue-arm64')\nraise SystemExit(17)\n")
    put(tmp_path/'bin/timeout','#!/bin/sh\nshift 5\nexec "$@"\n')
    env=dict(os.environ,PATH=str(tmp_path/'bin')+os.pathsep+os.environ['PATH'],CALL_LOG=str(tmp_path/'calls'),
             CHROMIX_USE_UPSTREAM_CACHE='0',CHROMIX_BUILD_PROFILE='fast',CHROMIX_LINUX157_LOCALE_RECOVERY='1',
             CHROMIX_LINUX157_ARM64_STAGE2_RECOVERY=enabled,CHROMIX_LINUX157_PRE_GN_RECOVERY='0',CHROMIX_LINUX157_POST_GN_RECOVERY='0')
    result=subprocess.run(['bash',str(tmp_path/'build/posix/ci-stage.sh'),'--platform','linux','--arch','arm64',
                           '--stage-index',str(stage),'--workdir',str(work)],cwd=tmp_path,env=env,capture_output=True,text=True)
    assert result.returncode!=0
    assert (tmp_path/'calls').exists() is expect_build
    assert (work/'src/out/Chromix/keep.o').read_bytes()==b'compiled-arm64'


def test_generated_stage3_restore_then_stage4_continuation(tmp_path):
    root=tmp_path.resolve();work=root/'chromix-build';(root/'chromix-logs').mkdir()
    calls=root/'calls';binary=root/'bin';binary.mkdir()
    put(binary/'timeout','#!/bin/sh\nshift 5\nexec "$@"\n')
    put(root/'CHROMIUM_LINUX_VERSION','157.0.8103.78\n')
    put(root/'build/posix/ci-stage.sh',(ROOT/'build/posix/ci-stage.sh').read_text())
    put(root/'tools/linux157_locale.py',"""import json,os,sys
from pathlib import Path
a=sys.argv;w=Path(a[a.index('--workdir')+1]);assert not w.exists()
assert a[a.index('--phase')+1]=='restore' and a[a.index('--arch')+1]=='arm64'
(w/'src/out/Chromix').mkdir(parents=True);(w/'fingerprint-diagnostics').mkdir()
for n,v in [('args.gn','target_cpu=arm64;PGO=0'),('build.ninja','stage2 graph'),('.ninja_log','# ninja log v7'),('keep.o','ARM stage2 object')]:
 (w/'src/out/Chromix'/n).write_text(v)
(w/'src/.chromix-linux157-locale-migration.json').write_text('{\"arch\":\"arm64\",\"stage\":2,\"count\":228}')
(w/'fingerprint-diagnostics/source-final.json').write_text('{\"patch_count\":228}')
with open(os.environ['CALL_LOG'],'a') as f:f.write('restore-exact-stage2\\n')
""")
    put(root/'tools/linux157_source.py',"""import json,os,sys
from pathlib import Path
a=sys.argv;phase=a[a.index('--phase')+1]
if phase=='checkpoint':
 w=Path(a[a.index('--workdir')+1]);assert json.loads((w/'src/.chromix-linux157-locale-migration.json').read_text())=={'arch':'arm64','stage':2,'count':228}
print('{}')
""")
    put(root/'tools/verify_linux157_upload.py',"""import os,sys,json
from pathlib import Path
w=Path(sys.argv[sys.argv.index('--workdir')+1]);assert json.loads((w/'fingerprint-diagnostics/source-final.json').read_text())['patch_count']==228
with open(os.environ['CALL_LOG'],'a') as f:f.write('upload-gate\\n')
""")
    put(root/'tools/linux157_build.py',"""import os,sys
from pathlib import Path
w=Path(sys.argv[sys.argv.index('--workdir')+1]);assert (w/'src/out/Chromix/keep.o').read_text()=='ARM stage2 object'
with open(os.environ['CALL_LOG'],'a') as f:f.write('continue-ninja\\n')
raise SystemExit(17)
""")
    env=dict(os.environ,RUNNER_TEMP=str(root),GITHUB_WORKSPACE=str(root),CALL_LOG=str(calls),
             PATH=str(binary)+os.pathsep+os.environ['PATH'],CHROMIX_USE_UPSTREAM_CACHE='0',CHROMIX_BUILD_PROFILE='fast',
             CHROMIX_LINUX157_LOCALE_RECOVERY='1',CHROMIX_LINUX157_ARM64_STAGE2_RECOVERY='1',
             CHROMIX_LINUX157_PRE_GN_RECOVERY='0',CHROMIX_LINUX157_POST_GN_RECOVERY='0')
    wf=yaml.safe_load(arm.render());restore=next(s for s in wf['jobs']['posix-3']['steps'] if s.get('name')=='Restore exact ARM stage2 compiled locale checkpoint')
    result=subprocess.run(['bash','-c',restore['run']],cwd=root,env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    protected={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in (work/'src/out/Chromix').iterdir()}
    for stage in (3,4):
        result=subprocess.run(['bash',str(root/'build/posix/ci-stage.sh'),'--platform','linux','--arch','arm64',
                               '--stage-index',str(stage),'--workdir',str(work)],cwd=root,env=env,capture_output=True,text=True)
        assert result.returncode==1 and 'build script failed' in result.stderr
    assert calls.read_text().splitlines()==['restore-exact-stage2','upload-gate','continue-ninja','continue-ninja']
    for path,state in protected.items():assert (path.read_bytes(),path.stat().st_mtime_ns)==state


def test_real_arm_donor_two_file_replay_preserves_every_out_entry(tmp_path):
    donor=os.environ.get('CHROMIX_ARM_LOCALE_SRC')
    if not donor:pytest.skip('requires SHA-verified latest ARM stage2 restored tree')
    src=Path(donor);work=src.parent;manifest=locale.load(ROOT,'arm64')
    locale.prior.verify_files(work,manifest['files'])
    locale.prior.verify_files(src,manifest['source_receipt']['substituted_outputs'])
    locale.prior.verify_files(src,{p['target']:p['before'] for p in manifest['migration']['additions']})
    core=work/'tooling/ungoogled-chromium';rules=locale.arp._rules((core/'domain_regex.list').read_bytes())
    listed=set((core/'domain_substitution.list').read_text().splitlines());stack=[]
    entries=manifest['source_receipt']['chromix_selection']['patches']+[
        {'path':p['patch'],'sha256':p['sha256']} for p in manifest['migration']['additions']]
    for entry in entries:
        raw=locale.source.regular(ROOT,entry['path']).read_bytes()
        assert locale.source.digest(raw)==entry['sha256']
        data,targets=locale.arp.transform_patch(raw,listed,rules);stack.append((entry['path'],data,targets))
    before_out=locale.snapshot_outputs(src/'out')
    before,revised=locale.private_migration(src,stack,tmp_path)
    for pin in manifest['migration']['additions']:
        assert locale.source.digest(before[pin['target']][0])==pin['before']
        assert locale.source.digest(revised[pin['target']])==pin['after']
        assert (src/pin['target']).read_bytes()==before[pin['target']][0]
    assert locale.snapshot_outputs(src/'out')==before_out


def test_native_arm_gate_is_not_optional_on_failure_or_success():
    data=yaml.safe_load(arm.render());gate=data['jobs']['verify-linux-arm64']
    assert gate['runs-on']=='ubuntu-24.04-arm'
    assert 'failure' in gate['if'] and 'cancelled' in gate['if']
    names={s.get('name'):s for s in gate['steps']}
    runtime=names['Verify checksum and native launcher'];fingerprint=names['Native ARM64 fingerprint regression gate']
    assert 'continue-on-error' not in runtime and 'continue-on-error' not in fingerprint
    assert '--arch arm64 --runtime' in runtime['run']
    assert 'fingerprint_acceptance.py' in fingerprint['run']


@pytest.mark.parametrize('field,value',[('arch','x64'),('runner','ubuntu-22.04'),('linux157_locale_recovery','false'),('use_upstream_cache','true')])
def test_fixed_arm_workflow_guard_rejects_wrong_route(tmp_path,field,value):
    wf=yaml.safe_load(arm.render());guard=next(s for s in wf['jobs']['posix-3']['steps'] if s.get('name')=='Require fixed ARM stage2-to-stage3 continuation')
    values={'github.repository':'xiaozhou26/Chromix','github.event_name':'workflow_dispatch','github.ref':'refs/heads/main',
            'inputs.platform':'linux','inputs.arch':'arm64','inputs.runner':'ubuntu-24.04','inputs.artifact':'chromix-linux-arm64',
            'inputs.build_profile':'fast','inputs.use_upstream_cache':'false','inputs.linux157_locale_recovery':'true',
            'inputs.linux157_pre_gn_recovery':'false','inputs.linux157_post_gn_recovery':'false','inputs.resume_migration':'',
            'inputs.resume_run_id':'','inputs.resume_artifact_ids':'','inputs.resume_attempt':'1','inputs.max-stages':'8'}
    values['inputs.'+field]=value;script=guard['run']
    for key,val in values.items():script=script.replace('${{ '+key+' }}',val)
    assert '${{' not in script
    result=subprocess.run(['bash','-c',script],cwd=tmp_path,capture_output=True)
    assert result.returncode!=0 and not list(tmp_path.iterdir())
