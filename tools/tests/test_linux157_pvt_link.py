"""Latest source232 linking-tree admission without losing object or ThinLTO caches."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from tools import linux157_pvt_link as link
from tools import linux157_x64_pvt_link as wrapper
from tools import linux157_storage as storage
from tools import gen_linux157_x64_pvt_link_workflow as workflow
from tools import gen_linux157_x64_storage_workflow as previous
from tools.tests.test_linux157_pre_gn_workflow import put
from tools.tests.test_linux157_storage import patch

ROOT = link.source.ROOT


def test_exact_latest_stage2_identity_and_pending_review():
    m = link.load()
    assert (m['run_id'], m['job_id'], m['stage'], m['attempt'], m['continuation_stage']) == (44712280809, 136206704039, 2, 1, 3)
    assert m['head_sha'] == 'fa32bca2d2c67e04c5ababa434e03941ed8ac1d0'
    assert m['artifact']['id'] == 14624172635 and m['artifact']['size_in_bytes'] == 4502236810
    assert m['artifact']['digest'] == 'sha256:318b1c7b4f79d38e20629a5d533612b6c6586f4476c054b9e470f25a096c64b3'
    assert m['source_final']['patch_count'] == 232
    assert len(m['source_receipt']['chromix_selection']['patches']) == 232
    assert m['storage_proof']['new_patch_count'] == 232
    assert m['migration']['new_patch_count'] == 233
    assert m['failure']['candidate_source'] == link.TARGET
    assert m['failure']['completed_targets'] == m['failure']['total_targets'] == 7557
    assert m['target_recipe'] == link.source.recipe()
    assert wrapper.MANIFEST not in link.source.recipe()['inputs']


def test_pending_review_refuses_before_tree_write(tmp_path):
    if link.load()['migration']['status'] == 'verified': pytest.skip('parent has sealed actual patch review')
    with pytest.raises(ValueError, match='strict parent review'):
        link.admit(tmp_path, 'x64', link.expected('x64'))
    with pytest.raises(ValueError, match='strict parent review'):
        wrapper.authenticate()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('field', ['run_id','job_id','stage','attempt','head_sha','arch'])
def test_old_stage1_or_wrong_donor_rejected_before_io(tmp_path, field):
    metadata = link.expected('x64')
    metadata[field] = 'wrong' if field in ('head_sha','arch') else metadata[field] + 1
    with pytest.raises(ValueError, match='tuple'): link.admit(tmp_path, 'x64', metadata)
    assert not list(tmp_path.iterdir())


def client(attack=None):
    location=os.environ.get('CHROMIX_PVT_LINK_AUDIT')
    if not location: pytest.skip('requires actual latest link-failed stage2 metadata')
    root=Path(location)
    run=json.loads((root/'run.json').read_bytes())
    jobs=json.loads((root/'jobs.json').read_bytes())['jobs']
    artifacts=json.loads((root/'artifacts.json').read_bytes())['artifacts']
    j=next(j for j in jobs if j['id']==136206704039)
    a=next(a for a in artifacts if a['id']==14624172635)
    if attack=='sha': a['digest']='sha256:'+'0'*64
    elif attack=='size': a['size_in_bytes']-=1
    elif attack=='head': run['head_sha']='0'*40
    elif attack=='attempt': j['run_attempt']=2
    elif attack=='job': j['id']=136124135111
    elif attack=='stage': a['name']=a['name'].replace('-s2-','-s1-')
    elif attack=='arch': a['name']=a['name'].replace('x64','arm64')
    elif attack=='preserve': next(s for s in j['steps'] if s['name']=='Preserve latest failed Linux source/output checkpoint')['conclusion']='failure'
    class Client:
        def get(self,path): return run
        def items(self,path,key): return jobs if key=='jobs' else artifacts
    return Client()


@pytest.mark.parametrize('attack',[None,'sha','size','head','attempt','job','stage','arch','preserve'])
def test_actual_metadata_requires_latest_link_tree(attack):
    if attack:
        with pytest.raises(ValueError): link.validate_metadata(client(attack),'x64')
    else:
        assert link.validate_metadata(client(),'x64')==link.expected('x64')


def fixture(root):
    src=root/'src';src.mkdir();stack=[]
    for i in range(232):
        name=f'old/{i}.cc';put(src/name,f'patched{i}\n')
        _,data,entries=patch(name,f'original{i}\n',f'patched{i}\n')
        stack.append((f'old/{i}.patch',data,entries))
    before='include PVT\nDependsOn(PVTFactory);\nkeep();\n';after='keep();\n'
    put(src/link.TARGET,before)
    raw,data,entries=patch(link.TARGET,before,after)
    name='patches/chromium157-additions/0242-fixture.patch'
    stack.append((name,data,entries))
    m={'migration':{'status':'verified','old_patch_count':232,'new_patch_count':233,
                   'additions':[{'patch':name,'sha256':link.source.digest(raw),'targets':[link.TARGET]}],
                   'sources':{link.TARGET:{'before':link.source.digest(before.encode()),'after':link.source.digest(after.encode())}}}}
    for name,raw in [('thinlto-cache/llvmcache-object','warm ThinLTO'),('obj/keep.o','compiled object'),
                     ('.ninja_log','# ninja log v7'),('.ninja_deps','deps'),('args.gn','chrome_pgo_phase=0'),
                     ('build.ninja','graph'),('chrome','failed link leftover')]:
        put(src/'out/Chromix'/name,raw)
    return src,stack,m


def test_full232_233_private_roundtrip_preserves_linkcache(tmp_path):
    src,stack,m=fixture(tmp_path);out=link.snapshot_outputs(src/'out')
    before,after=link.private_migration(src,stack,tmp_path,m)
    assert {n for n in after if after[n]!=before[n][0]}=={link.TARGET}
    assert link.snapshot_outputs(src/'out')==out
    for name,(raw,info) in before.items():
        assert (src/name).read_bytes()==raw
        assert (src/name).stat().st_mtime_ns==info.st_mtime_ns


def test_atomic_publication_preserves_all_link_outputs_and_stays_blocked(tmp_path):
    src,stack,m=fixture(tmp_path);out=link.snapshot_outputs(src/'out')
    before,after=link.private_migration(src,stack,tmp_path,m)
    with pytest.raises(ValueError): link.publish_sources(src,before,after,m['migration']['sources'])
    for n in (link.PENDING,'.chromix-layer-in-progress'): (src/n).write_text('pending')
    link.publish_sources(src,before,after,m['migration']['sources'])
    assert (src/link.TARGET).read_bytes()==after[link.TARGET]
    assert link.snapshot_outputs(src/'out')==out
    assert (src/link.PENDING).exists() and not (src/link.MARKER).exists()


def test_linkcache_same_size_same_mtime_mutation_is_detected(tmp_path):
    src,_,_=fixture(tmp_path);before=link.snapshot_outputs(src/'out')
    path=src/'out/Chromix/thinlto-cache/llvmcache-object';info=path.stat()
    path.write_bytes(b'x'*info.st_size);os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns))
    assert link.snapshot_outputs(src/'out')!=before


@pytest.mark.parametrize('stage,arch,optin,allowed',[(1,'x64','1',False),(2,'x64','1',False),(3,'arm64','1',False),(3,'x64','0',False),(3,'x64','1',True),(8,'x64','1',True)])
def test_failed_link_never_finished_from_existing_chrome(tmp_path,stage,arch,optin,allowed):
    work=tmp_path/'work';put(work/'src/out/Chromix/chrome','partial linked chrome');put(work/'src/out/Chromix/thinlto-cache/keep','warm')
    put(tmp_path/'CHROMIUM_LINUX_VERSION','157.0.8103.78\n')
    put(tmp_path/'build/posix/ci-stage.sh',(ROOT/'build/posix/ci-stage.sh').read_text())
    put(tmp_path/'tools/linux157_x64_pvt_link.py',"import sys,os\nfrom pathlib import Path\na=sys.argv\nif a[a.index('--command')+1]=='build':\n Path(os.environ['CALL_LOG']).write_text('link failed')\n raise SystemExit(17)\nprint('{}')\n")
    put(tmp_path/'bin/timeout','#!/bin/sh\nshift 5\nexec "$@"\n')
    env=dict(os.environ,PATH=str(tmp_path/'bin')+os.pathsep+os.environ['PATH'],CALL_LOG=str(tmp_path/'calls'),GITHUB_OUTPUT=str(tmp_path/'outputs'),
             CHROMIX_USE_UPSTREAM_CACHE='0',CHROMIX_BUILD_PROFILE='fast',CHROMIX_LINUX157_X64_PVT_LINK_RECOVERY='1',CHROMIX_LINUX157_X64_PVT_LINK_STAGE2_RECOVERY=optin,
             CHROMIX_LINUX157_X64_STORAGE_RECOVERY='0',CHROMIX_LINUX157_STORAGE_RECOVERY='0',CHROMIX_LINUX157_UA_RECOVERY='0',CHROMIX_LINUX157_PRE_GN_RECOVERY='0',CHROMIX_LINUX157_POST_GN_RECOVERY='0',CHROMIX_LINUX157_LOCALE_RECOVERY='0')
    result=subprocess.run(['bash',str(tmp_path/'build/posix/ci-stage.sh'),'--platform','linux','--arch',arch,'--stage-index',str(stage),'--workdir',str(work)],cwd=tmp_path,env=env,capture_output=True,text=True)
    assert result.returncode!=0 and (tmp_path/'calls').exists() is allowed
    if allowed: assert 'build script failed' in result.stderr
    assert 'finished=true' not in ((tmp_path/'outputs').read_text() if (tmp_path/'outputs').exists() else '')
    assert (work/'src/out/Chromix/chrome').read_text()=='partial linked chrome'
    assert (work/'src/out/Chromix/thinlto-cache/keep').read_text()=='warm'


def test_generated_stage3_through8_preserves_bounded_apt_and_failure_snapshot():
    text=workflow.render();assert text==(ROOT/workflow.OUT).read_text()
    jobs=yaml.safe_load(text)['jobs'];assert set(jobs)=={f'posix-{i}' for i in range(3,9)}
    assert 'needs' not in jobs['posix-3']
    old=yaml.safe_load(previous.render())['jobs']['posix-2']['steps'];apt=next(s for s in old if s.get('name')=='Install Linux build dependencies')
    for stage in range(3,9):
        j=jobs[f'posix-{stage}'];steps=j['steps']
        assert next(s for s in steps if s.get('name')==apt['name'])==apt
        if stage>3: assert j['needs']==f'posix-{stage-1}'
        run=next(s for s in steps if s.get('id')=='stage')
        assert f'--stage-index {stage}' in run['run']
        assert run['env']['CHROMIX_LINUX157_X64_PVT_LINK_STAGE2_RECOVERY']=='1'
        assert any(s.get('name')=='Preserve latest failed Linux source/output checkpoint' for s in steps)
        assert '--command upload --archive-report' in next(s for s in steps if s.get('name')=='Verify handoff snapshot')['run']
    restore=next(s for s in jobs['posix-3']['steps'] if s.get('name')=='Restore exact x64 stage2 compiled PVT link checkpoint')
    assert 'linux157_x64_pvt_link.py --command migration --phase restore --arch x64' in restore['run']


@pytest.mark.parametrize('name',['tools/linux157_source.py','tools/linux157_build.py','tools/linux157_storage.py','tools/linux157_x64_storage.py',
    'build/linux/157.0.8103.78/arm64-storage-checkpoint.json','.github/workflows/build-linux157-arm64-storage-resume.yml','.github/workflows/build-linux157-arm64-storage-stages.yml'])
def test_running_arm_and_shared_recipe_bytes_frozen(name):
    assert (ROOT/name).read_bytes()==subprocess.check_output(['git','-C',str(ROOT),'show','e411ccf25cae004b9e6f5f7815e003b3efc3a9b8:'+name])


def test_actual_stage2_source_and_nested_storage_chain():
    location=os.environ.get('CHROMIX_PVT_LINK_DONOR')
    if not location: pytest.skip('requires exact authenticated latest stage2 donor')
    work=Path(location);m=link.load();link.prior.verify_files(work,m['files'])
    link.prior.verify_files(work/'src',m['source_final']['outputs'])
    link.prior.verify_files(work/'src',m['source_receipt']['substituted_outputs'])
    link.verify_storage_chain(work,m)
    assert link.output_identity(link.snapshot_outputs(work/'src/out'))==m['output_tree']


@pytest.mark.parametrize('failure,code,commands',[('none',0,['update','install']),('update-error',100,['update']),('install-error',100,['update','install']),('update-timeout',124,['update']),('install-timeout',124,['update','install'])])
def test_bounded_apt_executes_and_blocks_failed_build(tmp_path,failure,code,commands):
    from tools.tests import test_linux157_ua as original
    class Workflow:
        @staticmethod
        def render():
            data=yaml.safe_load(workflow.render())
            data['jobs']['posix-1']=copy.deepcopy(data['jobs']['posix-3'])
            data['jobs']['posix-2']=copy.deepcopy(data['jobs']['posix-3'])
            data['jobs']['posix-2']['if']="needs.posix-1.result == 'success'"
            data['jobs']['posix-3']['if']="needs.posix-2.result == 'success'"
            return yaml.safe_dump(data)
    saved=original.workflow
    try:
        original.workflow=Workflow
        original.test_generated_apt_commands_fail_closed(tmp_path,failure,code,commands)
    finally: original.workflow=saved


@pytest.mark.parametrize('field,value',[('arch','arm64'),('runner','ubuntu-24.04'),('linux157_pvt_link_recovery','false'),('use_upstream_cache','true'),('resume_run_id','44712280809'),('resume_attempt','2')])
def test_fixed_stage3_guard_rejects_drift(tmp_path,field,value):
    steps=yaml.safe_load(workflow.render())['jobs']['posix-3']['steps']
    guard=next(s for s in steps if s.get('name')=='Require fixed x64 stage2-to-stage3 PVT link continuation')
    values={'github.repository':'xiaozhou26/Chromix','github.event_name':'workflow_dispatch','github.ref':'refs/heads/main',
            'inputs.platform':'linux','inputs.arch':'x64','inputs.runner':'ubuntu-22.04','inputs.artifact':'chromix-linux-x64',
            'inputs.build_profile':'fast','inputs.use_upstream_cache':'false','inputs.linux157_pvt_link_recovery':'true',
            'inputs.linux157_pre_gn_recovery':'false','inputs.linux157_post_gn_recovery':'false','inputs.resume_migration':'',
            'inputs.resume_run_id':'','inputs.resume_artifact_ids':'','inputs.resume_attempt':'1','inputs.max-stages':'8'}
    values['inputs.'+field]=value;script=guard['run']
    for key,val in values.items():script=script.replace('${{ '+key+' }}',val)
    assert '${{' not in script
    result=subprocess.run(['bash','-c',script],cwd=tmp_path,capture_output=True)
    assert result.returncode!=0 and not list(tmp_path.iterdir())


def test_scope_cannot_target_generated_output_or_extra_source(tmp_path):
    _,_,manifest=fixture(tmp_path)
    manifest['migration']['sources']['out/Chromix/build.ninja']=manifest['migration']['sources'].pop(link.TARGET)
    manifest['migration']['additions'][0]['targets']=['out/Chromix/build.ninja']
    with pytest.raises(ValueError):link.review(manifest)


def test_wrapper_restores_shared_arm_defaults_after_error(monkeypatch):
    original=storage.verify_current,storage.verify_current_ninja,link.load
    monkeypatch.setattr(wrapper,'authenticate',lambda repo: None)
    with pytest.raises(RuntimeError):
        with wrapper.profile():
            assert link.load()['stage']==2
            raise RuntimeError('fixture interruption')
    assert (storage.verify_current,storage.verify_current_ninja,link.load)==original
    assert storage.load()['arch']=='arm64'


def test_real_source232_nested_chain_cannot_omit_original_outputs():
    location=os.environ.get('CHROMIX_PVT_LINK_AUDIT')
    if not location: pytest.skip('requires actual authenticated source receipts')
    work=Path(location)/'logs/chromix-build';m=copy.deepcopy(link.load())
    # Full source after-images live in the tree; missing original proof is rejected first.
    m['storage_chain'].pop('linux157-storage-origin/output-hashes.json',None)
    with pytest.raises(ValueError,match='incomplete compiled Storage chain'):
        link.verify_storage_chain(work,m)


def test_recipe_cannot_drift_when_only_profile_changes():
    m=link.load();link.recipe_delta(m['source_receipt']['recipe'],link.source.recipe())
    bad=copy.deepcopy(link.source.recipe());bad['sha256']='0'*64
    with pytest.raises(ValueError):link.recipe_delta(m['source_receipt']['recipe'],bad)


def test_transaction_failure_preserves_pending_and_linkcache(tmp_path,monkeypatch):
    src,stack,m=fixture(tmp_path);before,after=link.private_migration(src,stack,tmp_path,m)
    for name in (link.PENDING,'.chromix-layer-in-progress'):(src/name).write_text('pending')
    outputs=link.snapshot_outputs(src/'out')
    def fail(*args,**kwargs):raise OSError('publication failure')
    monkeypatch.setattr(link,'atomic_bytes',fail)
    with pytest.raises(OSError):link.publish_sources(src,before,after,m['migration']['sources'])
    assert (src/link.TARGET).read_bytes()==before[link.TARGET][0]
    assert (src/link.PENDING).exists() and (src/'.chromix-layer-in-progress').exists()
    assert not (src/link.MARKER).exists()
    assert link.snapshot_outputs(src/'out')==outputs


def test_pending_restore_never_downloads_even_if_chrome_exists(tmp_path,monkeypatch):
    def forbidden(*args,**kwargs):pytest.fail('pending review attempted network or publication')
    monkeypatch.setattr(link.subprocess,'run',forbidden)
    with pytest.raises(ValueError,match='strict parent review'):link.restore(tmp_path/'absent','x64')
    assert not (tmp_path/'absent').exists()


def test_actual_archive_output_and_source232_proof():
    location=os.environ.get('CHROMIX_PVT_LINK_AUDIT')
    if not location:pytest.skip('requires completed actual stage2 archive audit')
    root=Path(location)
    if not (root/'archive-audit.json').exists():pytest.skip('actual archive audit not complete')
    report=json.loads((root/'archive-audit.json').read_bytes())
    outputs=json.loads((root/'archive-output-hashes.json').read_bytes())
    m=link.load();assert report['artifact']==m['artifact']
    assert link.output_identity(outputs)==m['output_tree']
    assert outputs['Chromix/.ninja_log']['content']==m['files']['src/out/Chromix/.ninja_log']
    assert outputs['Chromix/.ninja_deps']['content']==m['files']['src/out/Chromix/.ninja_deps']
    assert outputs['Chromix/build.ninja']['content']==m['files']['src/out/Chromix/build.ninja']
    work=root/'source-evidence-complete'
    link.prior.verify_files(work/'src',m['source_final']['outputs'])
    link.verify_storage_chain(work,m)
    assert link.tools.file_hash(work/'src'/link.TARGET)==m['failure']['candidate_source_sha256']
