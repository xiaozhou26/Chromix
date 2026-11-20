"""Independent x64 stage1 Storage continuation and immutable ARM recipe."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from tools import linux157_x64_storage as x64
from tools import linux157_storage as arm
from tools import gen_linux157_x64_storage_workflow as workflow
from tools import gen_linux157_ua_workflow as ua_workflow
from tools.tests.test_linux157_pre_gn_workflow import put

ROOT = x64.source.ROOT


def test_exact_x64_profile_and_unchanged_recipe():
    m = x64.load()
    assert (m['run_id'], m['job_id'], m['stage'], m['attempt'], m['continuation_stage']) == (44687163614, 136124135111, 1, 1, 2)
    assert m['arch'] == 'x64' and m['head_sha'] == '3da74546373f3351029064d79fe85e3d3cb4497f'
    assert m['artifact']['id'] == 14611658638 and m['artifact']['size_in_bytes'] == 4050703787
    assert m['artifact']['digest'] == 'sha256:788544fae90fef88a5687052724b0c1d9a9e131edebdbbbdc42f898f6f83380c'
    assert m['source_final']['patch_count'] == 229 and m['migration']['new_patch_count'] == 232
    assert m['target_recipe'] == x64.source.recipe()
    assert x64.source.recipe()['sha256'] == 'f72b67654204d0447a1f3732463382a6f0a7a16877ee35b2c51d00c22ca8adc2'
    assert x64.MANIFEST not in x64.source.recipe()['inputs']
    assert x64.HELPER not in x64.source.recipe()['inputs']
    assert arm.load()['run_id'] == 44660206036
    with pytest.raises(ValueError): x64.load(arch='arm64')


@pytest.mark.parametrize('name', ['tools/linux157_storage.py', 'tools/linux157_source.py', 'tools/linux157_build.py',
                                'build/linux/157.0.8103.78/arm64-storage-checkpoint.json',
                                '.github/workflows/build-linux157-arm64-storage-resume.yml',
                                '.github/workflows/build-linux157-arm64-storage-stages.yml',
                                'tools/gen_linux157_arm64_storage_workflow.py', 'tools/gen_linux157_ua_workflow.py'])
def test_arm_and_existing_recipe_bytes_frozen(name):
    old = subprocess.check_output(['git', '-C', str(ROOT), 'show', '461fe905610d3266171336d3b0a87657703991b7:' + name])
    assert (ROOT / name).read_bytes() == old


def test_parent_review_is_required_before_profile_switch():
    original = arm.load, arm.verify_current, arm.verify_current_ninja
    try:
        selected = arm.patch_selection.select(ROOT, 'linux')
        integration = selected[0]['selection']['downstream']['validation']['recipe_integration']
        if integration.get(x64.BINDING) == x64.binding():
            with x64.profile(): assert arm.load()['arch'] == 'x64'
        else:
            with pytest.raises(ValueError, match='parent selector review'):
                with x64.profile(): pytest.fail('unreviewed profile activated')
    finally:
        assert (arm.load, arm.verify_current, arm.verify_current_ninja) == original


def test_profile_context_restores_defaults_even_on_failure(monkeypatch):
    monkeypatch.setattr(x64, 'authenticate', lambda repo: None)
    original = arm.load, arm.verify_current, arm.verify_current_ninja
    with pytest.raises(RuntimeError):
        with x64.profile():
            assert arm.load()['arch'] == 'x64'
            with pytest.raises(ValueError): arm.load(arch='arm64')
            raise RuntimeError('fixture interruption')
    assert (arm.load, arm.verify_current, arm.verify_current_ninja) == original
    assert arm.load()['arch'] == 'arm64'


def metadata_client(attack=None):
    location = os.environ.get('CHROMIX_X64_STORAGE_AUDIT')
    if not location: pytest.skip('requires actual latest x64 metadata')
    root = Path(location)
    run = json.loads((root / 'run.json').read_bytes())
    jobs = json.loads((root / 'jobs.json').read_bytes())['jobs']
    artifacts = json.loads((root / 'artifacts.json').read_bytes())['artifacts']
    j = next(j for j in jobs if j['id'] == 136124135111)
    a = next(a for a in artifacts if a['id'] == 14611658638)
    if attack == 'sha': a['digest'] = 'sha256:' + '0'*64
    elif attack == 'size': a['size_in_bytes'] -= 1
    elif attack == 'head': run['head_sha'] = '0'*40
    elif attack == 'attempt': j['run_attempt'] = 2
    elif attack == 'job': j['id'] = 1
    elif attack == 'arch': a['name'] = a['name'].replace('x64', 'arm64')
    elif attack == 'stage': a['name'] = a['name'].replace('-s1-', '-s5-')
    elif attack == 'preserve': next(s for s in j['steps'] if s['name'] == 'Preserve latest failed Linux source/output checkpoint')['conclusion'] = 'failure'
    class Client:
        def get(self, path): return run
        def items(self, path, key): return jobs if key == 'jobs' else artifacts
    return Client()


@pytest.mark.parametrize('attack', [None, 'sha', 'size', 'head', 'attempt', 'job', 'arch', 'stage', 'preserve'])
def test_actual_x64_metadata_not_arm_substitute(monkeypatch, attack):
    monkeypatch.setattr(arm, 'load', x64.load)
    if attack:
        with pytest.raises(ValueError): arm.validate_metadata(metadata_client(attack), 'x64')
    else:
        want = arm.validate_metadata(metadata_client(), 'x64')
        assert want['job_id'] == 136124135111 and want['stage'] == 1


def test_generated_chain_routes_every_admission_through_wrapper():
    text = workflow.render()
    assert text == (ROOT / workflow.OUT).read_text()
    jobs = yaml.safe_load(text)['jobs']
    assert set(jobs) == {f'posix-{i}' for i in range(2, 9)}
    assert 'needs' not in jobs['posix-2']
    for stage in range(2, 9):
        job = jobs[f'posix-{stage}']
        if stage > 2: assert job['needs'] == f'posix-{stage-1}'
        steps = job['steps']
        run = next(s for s in steps if s.get('id') == 'stage')
        assert f'--stage-index {stage}' in run['run']
        assert run['env']['CHROMIX_LINUX157_X64_STORAGE_STAGE1_RECOVERY'] == '1'
        assert 'CHROMIX_LINUX157_STORAGE_RECOVERY' not in run['env']
        assert any(s.get('name') == 'Preserve latest failed Linux source/output checkpoint' for s in steps)
        for step in steps:
            assert 'python3 tools/linux157_source.py' not in step.get('run', '')
            assert 'python3 tools/verify_linux157_upload.py' not in step.get('run', '')
    restore = next(s for s in jobs['posix-2']['steps'] if s.get('name') == 'Restore exact x64 stage1 compiled Storage checkpoint')
    assert 'linux157_x64_storage.py --command storage --phase restore --arch x64' in restore['run']
    assert '--command source --phase checkpoint' in restore['run'] and '--command upload' in restore['run']


@pytest.mark.parametrize('stage,arch,optin,other,allowed', [(1,'x64','1','0',False),(2,'x64','0','0',False),
    (2,'arm64','1','0',False),(2,'x64','1','1',False),(2,'x64','1','0',True),(8,'x64','1','0',True)])
def test_ci_explicit_fixed_x64_stage2_route(tmp_path, stage, arch, optin, other, allowed):
    work = tmp_path / 'work'
    put(work / 'src/out/Chromix/keep.o', 'compiled x64 stage1')
    put(tmp_path / 'CHROMIUM_LINUX_VERSION', '157.0.8103.78\n')
    put(tmp_path / 'build/posix/ci-stage.sh', (ROOT / 'build/posix/ci-stage.sh').read_text())
    put(tmp_path / 'tools/linux157_x64_storage.py', '''import os,sys
from pathlib import Path
a=sys.argv
assert a[a.index('--arch')+1]=='x64'
command=a[a.index('--command')+1]
if command=='build':
 Path(os.environ['CALL_LOG']).write_text('x64-build')
 raise SystemExit(17)
assert command=='source'
print('{}')
''')
    put(tmp_path / 'bin/timeout', '#!/bin/sh\nshift 5\nexec "$@"\n')
    env = dict(os.environ, PATH=str(tmp_path / 'bin') + os.pathsep + os.environ['PATH'], CALL_LOG=str(tmp_path / 'calls'),
               CHROMIX_USE_UPSTREAM_CACHE='0', CHROMIX_BUILD_PROFILE='fast', CHROMIX_LINUX157_X64_STORAGE_RECOVERY='1',
               CHROMIX_LINUX157_X64_STORAGE_STAGE1_RECOVERY=optin, CHROMIX_LINUX157_STORAGE_RECOVERY=other,
               CHROMIX_LINUX157_UA_RECOVERY='0', CHROMIX_LINUX157_LOCALE_RECOVERY='0',
               CHROMIX_LINUX157_PRE_GN_RECOVERY='0', CHROMIX_LINUX157_POST_GN_RECOVERY='0')
    result = subprocess.run(['bash', str(tmp_path / 'build/posix/ci-stage.sh'), '--platform', 'linux', '--arch', arch,
                             '--stage-index', str(stage), '--workdir', str(work)], cwd=tmp_path, env=env, capture_output=True)
    assert result.returncode != 0 and (tmp_path / 'calls').exists() is allowed
    assert (work / 'src/out/Chromix/keep.o').read_text() == 'compiled x64 stage1'


def test_exact_bounded_apt_step_matches_previous_fixed_route_in_every_stage():
    previous = yaml.safe_load(ua_workflow.render())['jobs']['posix-1']['steps']
    dependencies = next(s for s in previous if s.get('name') == 'Install Linux build dependencies')
    for job in yaml.safe_load(workflow.render())['jobs'].values():
        assert next(s for s in job['steps'] if s.get('name') == dependencies['name']) == dependencies


@pytest.mark.parametrize('failure,code,commands', [('none',0,['update','install']),('update-error',100,['update']),
    ('install-error',100,['update','install']),('update-timeout',124,['update']),('install-timeout',124,['update','install'])])
def test_x64_bounded_apt_executes_and_fails_closed(tmp_path, failure, code, commands):
    from tools.tests import test_linux157_ua as original
    class Workflow:
        @staticmethod
        def render():
            data = yaml.safe_load(workflow.render())
            # The old executable fixture addresses its first job as posix-1.
            data['jobs']['posix-1'] = copy.deepcopy(data['jobs']['posix-2'])
            data['jobs']['posix-2']['if'] = "needs.posix-1.result == 'success'"
            return yaml.safe_dump(data)
    saved = original.workflow
    try:
        original.workflow = Workflow
        original.test_generated_apt_commands_fail_closed(tmp_path, failure, code, commands)
    finally:
        original.workflow = saved


def test_actual_x64_donor_hashes():
    location = os.environ.get('CHROMIX_X64_STORAGE_DONOR')
    if not location: pytest.skip('requires independently authenticated x64 stage1 donor')
    work = Path(location)
    m = x64.load()
    arm.prior.verify_files(work, m['files'])
    arm.prior.verify_files(work / 'src', m['source_receipt']['substituted_outputs'])
    arm.prior.verify_files(work / 'src', m['source_final']['outputs'])
    assert arm.output_identity(arm.snapshot_outputs(work / 'src/out')) == m['output_tree']


@pytest.mark.parametrize('field,value', [('arch','arm64'),('runner','ubuntu-24.04'),('linux157_storage_recovery','false'),('use_upstream_cache','true'),('resume_attempt','2'),('resume_run_id','44660206036')])
def test_fixed_x64_guard_rejects_drift(tmp_path, field, value):
    steps = yaml.safe_load(workflow.render())['jobs']['posix-2']['steps']
    guard = next(s for s in steps if s.get('name') == 'Require fixed x64 stage1-to-stage2 Storage continuation')
    values = {'github.repository':'xiaozhou26/Chromix','github.event_name':'workflow_dispatch','github.ref':'refs/heads/main',
              'inputs.platform':'linux','inputs.arch':'x64','inputs.runner':'ubuntu-22.04','inputs.artifact':'chromix-linux-x64',
              'inputs.build_profile':'fast','inputs.use_upstream_cache':'false','inputs.linux157_storage_recovery':'true',
              'inputs.linux157_pre_gn_recovery':'false','inputs.linux157_post_gn_recovery':'false','inputs.resume_migration':'',
              'inputs.resume_run_id':'','inputs.resume_artifact_ids':'','inputs.resume_attempt':'1','inputs.max-stages':'8'}
    values['inputs.'+field] = value
    script = guard['run']
    for key,val in values.items(): script=script.replace('${{ '+key+' }}',val)
    assert '${{' not in script
    result=subprocess.run(['bash','-c',script],cwd=tmp_path,capture_output=True)
    assert result.returncode != 0 and not list(tmp_path.iterdir())


def test_caller_has_no_donor_override_and_keeps_apt_runner():
    data=yaml.safe_load((ROOT/'.github/workflows/build-linux157-x64-storage-resume.yml').read_text())
    event=data.get('on',data.get(True))
    assert set(event['workflow_dispatch']['inputs']) == {'compile_jobs'}
    assert data['concurrency']['cancel-in-progress'] is False
    assert data['jobs']['resume']['with']['runner']=='ubuntu-22.04'
    assert data['jobs']['resume']['with']['linux157_storage_recovery'] is True


def test_actual_x64_migrated_cli_preserves_output_and_arm_defaults():
    donor=os.environ.get('CHROMIX_X64_STORAGE_MIGRATED')
    reviewed=os.environ.get('CHROMIX_X64_STORAGE_REVIEW_REPO')
    if not donor or not reviewed: pytest.skip('requires actual x64 one-phase migration and strict isolated review')
    repo=Path(reviewed)
    assert (repo/x64.HELPER).read_bytes()==(ROOT/x64.HELPER).read_bytes()
    for command,args in [('storage',['--phase','verify']),('source',['--phase','checkpoint'])]:
        result=subprocess.run([sys.executable,str(repo/x64.HELPER),'--command',command,'--repo',str(repo),
                               '--arch','x64','--workdir',donor,*args],cwd=repo,capture_output=True,text=True,check=True)
        proof=json.loads(result.stdout)
        assert proof['runtime_verified'] is False
    assert arm.output_identity(arm.snapshot_outputs(Path(donor)/'src/out')) == x64.load()['output_tree']
    assert arm.load()['arch']=='arm64'


def test_profile_binding_rejects_each_identity_change(monkeypatch):
    bound=x64.binding()
    def selection(value):
        return ({'selection':{'downstream':{'validation':{'recipe_integration':{x64.BINDING:value}}}}}, [])
    for field in ('helper','manifest','engine','recipe_sha256','run_id','arch','stage','route_inputs'):
        wrong=copy.deepcopy(bound)
        wrong[field]=None
        monkeypatch.setattr(arm.patch_selection,'select',lambda *args,v=wrong: selection(v))
        with pytest.raises(ValueError,match='parent selector review'):
            x64.authenticate()
    monkeypatch.setattr(arm.patch_selection,'select',lambda *args: selection(bound))
    x64.authenticate()


def test_explicit_wrapper_dispatches_source_build_upload_without_global_route_change(monkeypatch):
    from tools import linux157_build
    import tools.verify_linux157_upload as upload
    calls=[]
    monkeypatch.setattr(x64,'authenticate',lambda repo: None)
    monkeypatch.setattr(x64.source,'main',lambda args: calls.append(('source',args)) or 0)
    monkeypatch.setattr(linux157_build,'main',lambda args: calls.append(('build',args)) or 0)
    monkeypatch.setitem(sys.modules,'verify_linux157_upload',upload)
    monkeypatch.setattr(upload,'main',lambda args: calls.append(('upload',args)) or 0)
    for command in ('source','build','upload'):
        assert x64.main(['--command',command,'--arch','x64','--workdir','fixture'])==0
        assert arm.load()['arch']=='arm64'
    assert [c[0] for c in calls]==['source','build','upload']
    assert x64.main(['--command','build','--arch','arm64','--workdir','fixture'])==1
