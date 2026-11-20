"""Exact ARM stage6 linking-tree route, with frozen x64 engine and profiles."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from tools import linux157_arm64_pvt_link as arm
from tools import linux157_x64_pvt_link as x64
from tools import linux157_storage as shared
from tools import gen_linux157_arm64_pvt_link_workflow as workflow
from tools import gen_linux157_x64_pvt_link_workflow as previous
from tools import gen_posix_workflow as base
from tools.tests.test_linux157_pre_gn_workflow import put

ROOT=arm.source.ROOT
BASE='bebc7d38b6abab3ca8ed10c0dc06984ce0db0084'


def test_fixed_arm_stage6_donor_and_verified_single_source233():
    m=arm.load()
    assert (m['run_id'],m['job_id'],m['stage'],m['attempt'],m['continuation_stage'])==(44701383685,136170356964,6,1,7)
    assert m['head_sha']=='8147b39e782ed933ee2e9cb8b8e3fa734c53399b'
    assert m['artifact']['id']==14625498615 and m['artifact']['size_in_bytes']==5488969487
    assert m['artifact']['digest']=='sha256:fe2bd6ef09355c30becdc17b2e42afee51aaeab56842e3285585b7e6d29add33'
    assert m['arch']=='arm64' and m['source_final']['patch_count']==232
    assert arm.engine.review(m)['new_patch_count']==233
    assert m['migration']==x64.load()['migration']
    assert m['target_recipe']==arm.source.recipe()
    assert arm.source.recipe()['sha256']=='f72b67654204d0447a1f3732463382a6f0a7a16877ee35b2c51d00c22ca8adc2'
    with pytest.raises(ValueError):arm.load(arch='x64')


@pytest.mark.parametrize('name',['tools/linux157_pvt_link.py','tools/linux157_x64_pvt_link.py','build/linux/157.0.8103.78/x64-pvt-link-checkpoint.json',
    '.github/workflows/build-linux157-x64-pvt-link-resume.yml','.github/workflows/build-linux157-x64-pvt-link-stages.yml','tools/gen_linux157_x64_pvt_link_workflow.py',
    'build/posix/ci-stage.sh','tools/linux157_source.py','tools/linux157_build.py','tools/linux157_storage.py','tools/patch_selection.py','build/patch-selection-157.json'])
def test_x64_and_global_recipe_bytes_unchanged(name):
    assert (ROOT/name).read_bytes()==subprocess.check_output(['git','-C',str(ROOT),'show',BASE+':'+name])


def test_default_globals_restored_after_exception(monkeypatch):
    original=arm.engine.load,shared.verify_current,shared.verify_current_ninja
    monkeypatch.setattr(arm,'authenticate',lambda repo:None)
    with pytest.raises(RuntimeError):
        with arm.profile() as e:
            assert e.load()['arch']=='arm64' and e.load()['stage']==6
            with pytest.raises(ValueError):e.load(arch='x64')
            raise RuntimeError('interrupted fixture')
    assert (arm.engine.load,shared.verify_current,shared.verify_current_ninja)==original
    assert arm.engine.load()['arch']=='x64' and x64.load()['stage']==2


def test_parent_review_is_required():
    selected,_=arm.engine.patch_selection.select(ROOT,'linux')
    review=selected['selection']['downstream']['validation']['recipe_integration']
    if review.get(arm.BINDING)!=arm.binding():
        with pytest.raises(ValueError,match='parent selector review'):arm.authenticate()


def client(attack=None):
    location=os.environ.get('CHROMIX_ARM_PVT_AUDIT')
    if not location:pytest.skip('requires actual latest ARM stage6 metadata')
    root=Path(location);run=json.loads((root/'run.json').read_bytes());jobs=json.loads((root/'jobs.json').read_bytes())['jobs'];artifacts=json.loads((root/'artifacts.json').read_bytes())['artifacts']
    j=next(j for j in jobs if j['id']==136170356964);a=next(a for a in artifacts if a['id']==14625498615)
    if attack=='sha':a['digest']='sha256:'+'0'*64
    elif attack=='size':a['size_in_bytes']-=1
    elif attack=='head':run['head_sha']='0'*40
    elif attack=='job':j['id']=136206704039
    elif attack=='attempt':j['run_attempt']=2
    elif attack=='stage':a['name']=a['name'].replace('-s6-','-s5-')
    elif attack=='arch':a['name']=a['name'].replace('arm64','x64')
    elif attack=='preserve':next(s for s in j['steps'] if s['name']=='Preserve latest failed Linux source/output checkpoint')['conclusion']='failure'
    class Client:
        def get(self,p):return run
        def items(self,p,k):return jobs if k=='jobs' else artifacts
    return Client()


@pytest.mark.parametrize('attack',[None,'sha','size','head','job','attempt','stage','arch','preserve'])
def test_actual_metadata_not_x64_or_stage5(monkeypatch,attack):
    monkeypatch.setattr(arm.engine,'load',arm.load)
    if attack:
        with pytest.raises(ValueError):arm.engine.validate_metadata(client(attack),'arm64')
    else:assert arm.engine.validate_metadata(client(),'arm64')==arm.engine.expected('arm64')


def test_generated_stage7_8_and_native_arm_gate():
    text=workflow.render();assert text==(ROOT/workflow.OUT).read_text()
    jobs=yaml.safe_load(text)['jobs'];assert set(jobs)=={'posix-7','posix-8','verify-linux-arm64'}
    assert 'needs' not in jobs['posix-7'];assert jobs['posix-8']['needs']=='posix-7'
    old=next(s for s in yaml.safe_load(previous.render())['jobs']['posix-3']['steps'] if s.get('name')=='Install Linux build dependencies')
    for stage in (7,8):
        steps=jobs[f'posix-{stage}']['steps'];run=next(s for s in steps if s.get('id')=='stage')
        assert f'--stage-index {stage}' in run['run']
        assert run['env']['CHROMIX_LINUX157_ARM64_PVT_LINK_STAGE6_RECOVERY']=='1'
        assert 'CHROMIX_LINUX157_X64_PVT_LINK_RECOVERY' not in run['env']
        assert next(s for s in steps if s.get('name')==old['name'])==old
        assert any(s.get('name')=='Preserve latest failed Linux source/output checkpoint' for s in steps)
    restore=next(s for s in jobs['posix-7']['steps'] if s.get('name')=='Restore exact ARM stage6 compiled PVT link checkpoint')
    assert 'linux157_arm64_pvt_link.py --command migration --phase restore --arch arm64' in restore['run']
    native=jobs['verify-linux-arm64'];original=yaml.safe_load(base.NATIVE_LINUX_ARM64)['verify-linux-arm64']
    assert native['needs']==['posix-7','posix-8']
    assert {k:v for k,v in native.items() if k!='needs'}=={k:v for k,v in original.items() if k!='needs'}
    assert native['runs-on']=='ubuntu-24.04-arm'


@pytest.mark.parametrize('stage,arch,optin,other,allowed',[(6,'arm64','1','0',False),(7,'x64','1','0',False),(7,'arm64','0','0',False),(7,'arm64','1','1',False),(7,'arm64','1','0',True),(8,'arm64','1','0',True)])
def test_nonzero_link_with_existing_chrome_never_finished(tmp_path,stage,arch,optin,other,allowed):
    work=tmp_path/'work';put(work/'src/out/Chromix/chrome','partial ARM link');put(work/'src/out/Chromix/thinlto-cache/keep','warm ARM cache')
    put(tmp_path/'CHROMIUM_LINUX_VERSION','157.0.8103.78\n');put(tmp_path/'build/posix/ci-stage.sh',(ROOT/'build/posix/ci-stage-arm64-pvt-link.sh').read_text())
    put(tmp_path/'tools/linux157_arm64_pvt_link.py',"import sys,os\nfrom pathlib import Path\na=sys.argv\nassert a[a.index('--arch')+1]=='arm64'\nif a[a.index('--command')+1]=='build':\n Path(os.environ['CALL_LOG']).write_text('failed link')\n raise SystemExit(17)\nprint('{}')\n")
    put(tmp_path/'bin/timeout','#!/bin/sh\nshift 5\nexec "$@"\n')
    env=dict(os.environ,PATH=str(tmp_path/'bin')+os.pathsep+os.environ['PATH'],CALL_LOG=str(tmp_path/'calls'),GITHUB_OUTPUT=str(tmp_path/'outputs'),CHROMIX_BUILD_PROFILE='fast',CHROMIX_USE_UPSTREAM_CACHE='0',
             CHROMIX_LINUX157_ARM64_PVT_LINK_RECOVERY='1',CHROMIX_LINUX157_ARM64_PVT_LINK_STAGE6_RECOVERY=optin,CHROMIX_LINUX157_X64_PVT_LINK_RECOVERY=other,
             CHROMIX_LINUX157_X64_STORAGE_RECOVERY='0',CHROMIX_LINUX157_STORAGE_RECOVERY='0',CHROMIX_LINUX157_UA_RECOVERY='0',CHROMIX_LINUX157_LOCALE_RECOVERY='0',CHROMIX_LINUX157_PRE_GN_RECOVERY='0',CHROMIX_LINUX157_POST_GN_RECOVERY='0')
    result=subprocess.run(['bash',str(tmp_path/'build/posix/ci-stage.sh'),'--platform','linux','--arch',arch,'--stage-index',str(stage),'--workdir',str(work)],cwd=tmp_path,env=env,capture_output=True,text=True)
    assert result.returncode!=0 and (tmp_path/'calls').exists() is allowed
    if allowed:assert 'build script failed' in result.stderr
    assert 'finished=true' not in ((tmp_path/'outputs').read_text() if (tmp_path/'outputs').exists() else '')
    assert (work/'src/out/Chromix/thinlto-cache/keep').read_text()=='warm ARM cache'


@pytest.mark.parametrize('failure,code,commands',[('none',0,['update','install']),('update-error',100,['update']),('install-error',100,['update','install']),('update-timeout',124,['update']),('install-timeout',124,['update','install'])])
def test_arm_bounded_apt_executes_and_blocks(tmp_path,failure,code,commands):
    from tools.tests import test_linux157_ua as original
    class Workflow:
        @staticmethod
        def render():
            data=yaml.safe_load(workflow.render().replace('build/posix/ci-stage-arm64-pvt-link.sh','build/posix/ci-stage.sh'));first=data['jobs']['posix-7']
            for i in range(1,7):
                data['jobs'][f'posix-{i}']=copy.deepcopy(first)
                if i>1:data['jobs'][f'posix-{i}']['if']=f"needs.posix-{i-1}.result == 'success'"
            data['jobs']['posix-7']['if']="needs.posix-6.result == 'success'"
            return yaml.safe_dump(data)
    saved=original.workflow
    try:
        original.workflow=Workflow;original.test_generated_apt_commands_fail_closed(tmp_path,failure,code,commands)
    finally:original.workflow=saved


def test_actual_archive_and_source_provenance():
    location=os.environ.get('CHROMIX_ARM_PVT_AUDIT')
    if not location:pytest.skip('requires actual ARM stream audit')
    root=Path(location)
    if not (root/'archive-audit.json').exists():pytest.skip('actual stream not complete')
    m=arm.load();report=json.loads((root/'archive-audit.json').read_bytes());outputs=json.loads((root/'archive-output-hashes.json').read_bytes())
    assert report['artifact']==m['artifact'] and arm.engine.output_identity(outputs)==m['output_tree']
    assert report['local_output_inode_verified'] is False
    work=root/'source-evidence-complete';arm.engine.prior.verify_files(work/'src',m['source_final']['outputs']);arm.engine.verify_storage_chain(work,m,ROOT)
    assert arm.engine.tools.file_hash(work/'src'/arm.engine.TARGET)==m['migration']['sources'][arm.engine.TARGET]['before']


@pytest.mark.parametrize('field,value',[('arch','x64'),('runner','ubuntu-22.04'),('linux157_pvt_link_recovery','false'),('use_upstream_cache','true'),('resume_attempt','2'),('resume_run_id','44712280809')])
def test_fixed_arm_guard_rejects_drift(tmp_path,field,value):
    steps=yaml.safe_load(workflow.render())['jobs']['posix-7']['steps'];guard=next(s for s in steps if s.get('name')=='Require fixed ARM stage6-to-stage7 PVT link continuation')
    values={'github.repository':'xiaozhou26/Chromix','github.event_name':'workflow_dispatch','github.ref':'refs/heads/main',
            'inputs.platform':'linux','inputs.arch':'arm64','inputs.runner':'ubuntu-24.04','inputs.artifact':'chromix-linux-arm64',
            'inputs.build_profile':'fast','inputs.use_upstream_cache':'false','inputs.linux157_pvt_link_recovery':'true',
            'inputs.linux157_pre_gn_recovery':'false','inputs.linux157_post_gn_recovery':'false','inputs.resume_migration':'',
            'inputs.resume_run_id':'','inputs.resume_artifact_ids':'','inputs.resume_attempt':'1','inputs.max-stages':'8'}
    values['inputs.'+field]=value;text=guard['run']
    for key,val in values.items():text=text.replace('${{ '+key+' }}',val)
    assert '${{' not in text
    result=subprocess.run(['bash','-c',text],cwd=tmp_path,capture_output=True)
    assert result.returncode!=0 and not list(tmp_path.iterdir())


def test_caller_fixed_stage6_and_metadata_cli_restores_engine(monkeypatch,capsys):
    caller=yaml.safe_load((ROOT/'.github/workflows/build-linux157-arm64-pvt-link-resume.yml').read_text())
    event=caller.get('on',caller.get(True));assert set(event['workflow_dispatch']['inputs'])=={'compile_jobs'}
    assert caller['concurrency']['cancel-in-progress'] is False
    assert caller['jobs']['resume']['with']['arch']=='arm64'
    original=arm.engine.load
    monkeypatch.setenv('GH_TOKEN','fixture-token-no-network')
    def metadata(client,arch,repo):
        assert arch=='arm64' and arm.engine.load()['stage']==6
        return {'stage':6}
    monkeypatch.setattr(arm.engine,'validate_metadata',metadata)
    assert arm.main(['--command','migration','--phase','metadata','--arch','arm64'])==0
    assert arm.engine.load is original and json.loads(capsys.readouterr().out)=={'stage':6}


def test_binding_rejects_each_profile_identity_drift(monkeypatch):
    bound=arm.binding()
    for field in ('manifest','helper','engine','route_inputs','recipe_sha256','arch','run_id','job_id','stage','continuation_stage'):
        wrong=copy.deepcopy(bound);wrong[field]=None
        monkeypatch.setattr(arm.engine.patch_selection,'select',lambda *args,v=wrong:({'selection':{'downstream':{'validation':{'recipe_integration':{arm.BINDING:v}}}}},[]))
        with pytest.raises(ValueError,match='parent selector review'):arm.authenticate()


def test_actual_arm_source233_is_not_full_output_migration():
    location=os.environ.get('CHROMIX_ARM_PVT_AUDIT')
    if not location:pytest.skip('requires actual ARM233 source-only evidence')
    root=Path(location)
    if not (root/'source233-report.json').exists():pytest.skip('source-only replay not complete')
    report=json.loads((root/'source233-report.json').read_bytes())
    assert report['arch']=='arm64' and report['donor_run']==44701383685 and report['donor_job']==136170356964
    assert report['old_patch_count']==232 and report['new_patch_count']==233
    assert report['old_and_new_full_private_reverse_forward'] is True
    assert report['post_publication_full233_reverse_forward'] is True
    assert report['production_admit_called'] is False and report['full_output_migration_pass'] is False
    assert report['local_output_inode_verified'] is False
    work=Path(report['source_clone'])
    assert (work/'src'/arm.engine.PENDING).exists() and not (work/'src'/arm.engine.MARKER).exists()
    with pytest.raises(ValueError,match='incomplete storage migration'):arm.engine.verify(work,'arm64')


def test_shared_stage_byte_frozen_and_dedicated_stage_only_adds_arm_route():
    original=(ROOT/'build/posix/ci-stage.sh').read_text()
    dedicated=(ROOT/'build/posix/ci-stage-arm64-pvt-link.sh').read_text()
    start=dedicated.index('case "${CHROMIX_LINUX157_ARM64_PVT_LINK_RECOVERY:-0}" in')
    end=dedicated.index('case "${CHROMIX_LINUX157_X64_PVT_LINK_RECOVERY:-0}" in',start)
    assert dedicated[:start]+dedicated[end:]==original
    x64.authenticate()


def test_arm_metadata_failure_restores_x64_engine(monkeypatch):
    original=arm.engine.load
    monkeypatch.setenv('GH_TOKEN','fixture-token-no-network')
    def fail(*args):raise ValueError('metadata rejected')
    monkeypatch.setattr(arm.engine,'validate_metadata',fail)
    assert arm.main(['--command','migration','--phase','metadata','--arch','arm64'])==1
    assert arm.engine.load is original and arm.engine.load()['arch']=='x64'


def test_shared_production_gate_requires_complete_output_not_representative_subset(tmp_path):
    put(tmp_path/'Chromix/args.gn','target_cpu="arm64"')
    put(tmp_path/'Chromix/thinlto-cache/one','representative only')
    output=arm.engine.snapshot_outputs(tmp_path)
    m=arm.load()
    assert arm.engine.output_identity(output)!=m['output_tree']
    assert arm.HELPER not in arm.source.recipe()['inputs'] and arm.MANIFEST not in arm.source.recipe()['inputs']


def test_native_gate_has_mandatory_runtime_and_fingerprint_checks():
    native=yaml.safe_load(workflow.render())['jobs']['verify-linux-arm64']
    runtime=next(s for s in native['steps'] if s.get('name')=='Verify checksum and native launcher')
    fingerprint=next(s for s in native['steps'] if s.get('name')=='Native ARM64 fingerprint regression gate')
    assert '--arch arm64 --runtime' in runtime['run'] and 'continue-on-error' not in runtime
    assert 'fingerprint_acceptance.py' in fingerprint['run'] and 'continue-on-error' not in fingerprint


def test_dedicated_uploads_all_use_arm_wrapper():
    for name,job in yaml.safe_load(workflow.render())['jobs'].items():
        if name=='verify-linux-arm64':continue
        for step in job['steps']:
            script=step.get('run','')
            assert 'python3 tools/linux157_source.py' not in script
            assert 'python3 tools/verify_linux157_upload.py' not in script
        checkpoint=next(s for s in job['steps'] if s.get('name')=='Verify handoff snapshot')
        assert 'linux157_arm64_pvt_link.py --command upload --archive-report' in checkpoint['run']
