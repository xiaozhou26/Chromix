"""Latest ARM stage3 UA donor and logical-stage4 recovery without x64 drift."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from tools import linux157_ua as ua
from tools import linux157_source as source
from tools import gen_posix_workflow as base
from tools import gen_linux157_arm64_ua_workflow as arm
from tools.tests.test_linux157_pre_gn_workflow import put

ROOT = source.ROOT


def test_exact_arm_stage3_and_x64_profiles_are_distinct():
    x64, target = ua.expected('x64'), ua.expected('arm64')
    assert (x64['run_id'], x64['stage'], x64['artifacts'][0]['id']) == (44630942999, 1, 14584551831)
    assert (target['run_id'], target['stage'], target['attempt'], target['job_id']) == (44639133090, 3, 1, 135965300642)
    assert target['head_sha'] == '35e2db1b0687a4525017f5ec8fd3820823aef8ff'
    assert target['artifacts'][0]['id'] == 14594220250
    assert target['artifacts'][0]['size_in_bytes'] == 4381724895
    assert target['artifacts'][0]['digest'] == 'sha256:8054de376729ad6e28fc6d8797145b1b53b7f3bf009ed36744b993885ed4b2e8'
    m = ua.load(arch='arm64')
    assert m['continuation_stage'] == 4 and m['source_final']['patch_count'] == 228
    assert m['migration'] == ua.load()['migration']
    assert m['files']['src/out/Chromix/.ninja_log'] and m['files']['src/out/Chromix/.ninja_deps']


@pytest.mark.parametrize('field', ['run_id', 'stage', 'attempt', 'job_id', 'head_sha', 'arch'])
def test_wrong_tuple_rejects_before_tree_access(tmp_path, field):
    metadata = ua.expected('arm64')
    metadata[field] = 'wrong' if field in ('head_sha', 'arch') else metadata[field] + 1
    with pytest.raises(ValueError, match='tuple'):
        ua.admit(tmp_path, 'arm64', metadata)
    assert not list(tmp_path.iterdir())


def client(attack=None):
    audit = os.environ.get('CHROMIX_ARM_UA_AUDIT')
    if not audit:
        pytest.skip('requires exact latest stage3 GitHub metadata')
    root = Path(audit)
    run = json.loads((root/'run.json').read_text())
    jobs = json.loads((root/'jobs.json').read_text())['jobs']
    artifacts = json.loads((root/'artifacts.json').read_text())['artifacts']
    job = next(j for j in jobs if j['id'] == 135965300642)
    artifact = next(a for a in artifacts if a['id'] == 14594220250)
    if attack == 'attempt': run['run_attempt'] = 2
    elif attack == 'sha': artifact['digest'] = 'sha256:'+'0'*64
    elif attack == 'head': run['head_sha'] = '0'*40
    elif attack == 'job': job['id'] = 1
    elif attack == 'stage': artifact['name'] = artifact['name'].replace('-s3-', '-s2-')
    elif attack == 'arch': artifact['name'] = artifact['name'].replace('-arm64-', '-x64-')
    elif attack == 'step': next(s for s in job['steps'] if s['name'] == 'Run stage 3')['name'] = 'Run stage 2'
    class Client:
        def get(self, path): return run
        def items(self, path, key): return jobs if key == 'jobs' else artifacts
    return Client()


def test_actual_metadata_accepts_latest_stage3():
    assert ua.validate_metadata(client(), 'arm64') == ua.expected('arm64')


@pytest.mark.parametrize('attack', ['attempt', 'sha', 'head', 'job', 'stage', 'arch', 'step'])
def test_actual_metadata_mutations_fail_closed(attack):
    with pytest.raises(ValueError):
        ua.validate_metadata(client(attack), 'arm64')


def test_recipe_delta_specific_to_each_donor():
    from tools import linux157_storage
    historical = linux157_storage.load()['source_receipt']['recipe']
    for arch in ('x64', 'arm64'):
        old = ua.load(arch=arch)['source_receipt']['recipe']
        ua.recipe_delta(old, historical, arch=arch)
        bad = copy.deepcopy(historical)
        bad['inputs']['tools/linux157_locale.py'] = '0'*64
        with pytest.raises(ValueError): ua.recipe_delta(old, bad, arch=arch)
        with pytest.raises(ValueError): ua.recipe_delta(old, source.recipe(), arch=arch)
    with pytest.raises(ValueError):
        ua.recipe_delta(ua.load(arch='arm64')['source_receipt']['recipe'], historical, arch='x64')


def test_generated_stage4_chain_and_native_gate():
    text = arm.render()
    assert text == (ROOT/arm.OUT).read_text()
    jobs = yaml.safe_load(text)['jobs']
    assert set(jobs) == {*(f'posix-{i}' for i in range(4,9)), 'verify-linux-arm64'}
    assert 'needs' not in jobs['posix-4']
    assert jobs['posix-5']['needs'] == 'posix-4'
    for stage in range(4,9):
        steps = jobs[f'posix-{stage}']['steps']
        run = next(s for s in steps if s.get('id') == 'stage')
        assert f'--stage-index {stage}' in run['run']
        assert run['env']['CHROMIX_LINUX157_ARM64_UA_STAGE3_RECOVERY'] == '1'
        assert any(s.get('name') == 'Preserve latest failed Linux source/output checkpoint' for s in steps)
        upload = next(s for s in steps if s.get('name') == 'Upload tree part 1')
        assert f'-tree-s{stage}-attempt-' in upload['with']['name']
    first = jobs['posix-4']['steps']
    assert not any(s.get('name') == 'Download tree from previous stage' for s in first)
    restore = next(s for s in first if s.get('name') == 'Restore exact ARM stage3 compiled UA checkpoint')
    assert 'linux157_ua.py --phase restore --arch arm64' in restore['run']
    original = yaml.safe_load(base.NATIVE_LINUX_ARM64)['verify-linux-arm64']
    native = jobs['verify-linux-arm64']
    assert native['needs'] == [f'posix-{i}' for i in range(4,9)]
    assert {k:v for k,v in native.items() if k!='needs'} == {k:v for k,v in original.items() if k!='needs'}


def test_fixed_caller_cannot_cancel_x64():
    data = yaml.safe_load((ROOT/'.github/workflows/build-linux157-arm64-ua-resume.yml').read_text())
    events = data.get('on', data.get(True))
    assert set(events) == {'workflow_dispatch'}
    assert set(events['workflow_dispatch']['inputs']) == {'compile_jobs'}
    assert data['concurrency']['cancel-in-progress'] is False
    assert 'arm64-ua-resume' in data['concurrency']['group']
    assert data['jobs']['resume']['with']['arch'] == 'arm64'


@pytest.mark.parametrize('stage,optin,allowed', [(1,'1',False),(3,'1',False),(4,'0',False),(4,'1',True),(5,'1',True)])
def test_ci_requires_explicit_latest_arm_stage4(tmp_path, stage, optin, allowed):
    work=tmp_path/'work';(work/'src/out/Chromix').mkdir(parents=True)
    (work/'src/out/Chromix/keep.o').write_bytes(b'ARM stage3 compiled object')
    put(tmp_path/'CHROMIUM_LINUX_VERSION','157.0.8103.78\n')
    put(tmp_path/'build/posix/ci-stage.sh',(ROOT/'build/posix/ci-stage.sh').read_text())
    put(tmp_path/'tools/linux157_source.py',"print('{}')\n")
    put(tmp_path/'tools/linux157_build.py',"import os\nfrom pathlib import Path\nPath(os.environ['CALL_LOG']).write_text('continue')\nraise SystemExit(17)\n")
    put(tmp_path/'bin/timeout','#!/bin/sh\nshift 5\nexec "$@"\n')
    env=dict(os.environ, PATH=str(tmp_path/'bin')+os.pathsep+os.environ['PATH'], CALL_LOG=str(tmp_path/'calls'),
             CHROMIX_USE_UPSTREAM_CACHE='0', CHROMIX_BUILD_PROFILE='fast', CHROMIX_LINUX157_UA_RECOVERY='1',
             CHROMIX_LINUX157_ARM64_UA_STAGE3_RECOVERY=optin, CHROMIX_LINUX157_LOCALE_RECOVERY='0',
             CHROMIX_LINUX157_PRE_GN_RECOVERY='0', CHROMIX_LINUX157_POST_GN_RECOVERY='0')
    result=subprocess.run(['bash',str(tmp_path/'build/posix/ci-stage.sh'),'--platform','linux','--arch','arm64',
                           '--workdir',str(work),'--stage-index',str(stage)],cwd=tmp_path,env=env,capture_output=True)
    assert result.returncode != 0 and (tmp_path/'calls').exists() is allowed
    assert (work/'src/out/Chromix/keep.o').read_bytes() == b'ARM stage3 compiled object'


def test_actual_stage3_private_replay_no_selector_bypass(tmp_path):
    donor=os.environ.get('CHROMIX_ARM_UA_SRC')
    if not donor: pytest.skip('requires SHA-authenticated latest ARM stage3 donor')
    src=Path(donor);work=src.parent;m=ua.load(arch='arm64')
    ua.prior.verify_files(work,m['files'])
    ua.prior.verify_files(src,m['source_receipt']['substituted_outputs'])
    core=work/'tooling/ungoogled-chromium';listed=set((core/'domain_substitution.list').read_text().splitlines())
    rules=ua.arp._rules((core/'domain_regex.list').read_bytes());stack=[]
    entries=m['source_receipt']['chromix_selection']['patches']+[
        {'path':p['patch'],'sha256':p['sha256']} for p in m['migration']['additions']]
    for entry in entries:
        raw=source.regular(ROOT,entry['path']).read_bytes()
        assert source.digest(raw)==entry['sha256']
        data,targets=ua.arp.transform_patch(raw,listed,rules);stack.append((entry['path'],data,targets))
    protected=ua.snapshot_outputs(src/'out')
    before,after=ua.private_migration(src,stack,tmp_path)
    for pin in m['migration']['additions']:
        assert source.digest(before[pin['target']][0])==pin['before']
        assert source.digest(after[pin['target']])==pin['after']
        assert (src/pin['target']).read_bytes()==before[pin['target']][0]
    assert ua.snapshot_outputs(src/'out') == protected


def test_generated_arm_restore_stage4_then_stage5_keeps_compiled_receipts(tmp_path):
    root=tmp_path.resolve();work=root/'chromix-build';(root/'chromix-logs').mkdir();calls=root/'calls'
    put(root/'CHROMIUM_LINUX_VERSION','157.0.8103.78\n')
    put(root/'build/posix/ci-stage.sh',(ROOT/'build/posix/ci-stage.sh').read_text())
    put(root/'bin/timeout','#!/bin/sh\nshift 5\nexec "$@"\n')
    put(root/'tools/linux157_ua.py',"""import json,os,sys
from pathlib import Path
a=sys.argv;w=Path(a[a.index('--workdir')+1]);assert not w.exists()
assert a[a.index('--phase')+1]=='restore' and a[a.index('--arch')+1]=='arm64'
(w/'src/out/Chromix').mkdir(parents=True);(w/'fingerprint-diagnostics').mkdir()
for n,v in [('args.gn','ARM PGO=0'),('build.ninja','stage3 graph'),('.ninja_log','# ninja log v7'),('keep.o','7997 more targets')]:
 (w/'src/out/Chromix'/n).write_text(v)
(w/'src/.chromix-linux157-locale-migration.json').write_text('keep ICU proof')
(w/'src/.chromix-linux157-ua-migration.json').write_text('{\"arch\":\"arm64\",\"stage\":3,\"count\":229}')
(w/'fingerprint-diagnostics/source-final.json').write_text('{\"patch_count\":229}')
with open(os.environ['CALL_LOG'],'a') as f:f.write('restore-exact-stage3\\n')
""")
    put(root/'tools/linux157_source.py',"""import json,os,sys
from pathlib import Path
a=sys.argv
if a[a.index('--phase')+1]=='checkpoint':
 w=Path(a[a.index('--workdir')+1]);assert json.loads((w/'src/.chromix-linux157-ua-migration.json').read_text())=={'arch':'arm64','stage':3,'count':229}
print('{}')
""")
    put(root/'tools/verify_linux157_upload.py',"""import json,os,sys
from pathlib import Path
w=Path(sys.argv[sys.argv.index('--workdir')+1]);assert json.loads((w/'fingerprint-diagnostics/source-final.json').read_text())['patch_count']==229
with open(os.environ['CALL_LOG'],'a') as f:f.write('actual-upload-gate\\n')
""")
    put(root/'tools/linux157_build.py',"""import os,sys
from pathlib import Path
w=Path(sys.argv[sys.argv.index('--workdir')+1]);assert (w/'src/out/Chromix/keep.o').read_text()=='7997 more targets'
assert (w/'src/.chromix-linux157-locale-migration.json').read_text()=='keep ICU proof'
with open(os.environ['CALL_LOG'],'a') as f:f.write('continue-arm-ninja\\n')
raise SystemExit(17)
""")
    env=dict(os.environ,RUNNER_TEMP=str(root),GITHUB_WORKSPACE=str(root),CALL_LOG=str(calls),PATH=str(root/'bin')+os.pathsep+os.environ['PATH'],
             CHROMIX_USE_UPSTREAM_CACHE='0',CHROMIX_BUILD_PROFILE='fast',CHROMIX_LINUX157_UA_RECOVERY='1',
             CHROMIX_LINUX157_ARM64_UA_STAGE3_RECOVERY='1',CHROMIX_LINUX157_LOCALE_RECOVERY='0',
             CHROMIX_LINUX157_PRE_GN_RECOVERY='0',CHROMIX_LINUX157_POST_GN_RECOVERY='0')
    wf=yaml.safe_load(arm.render());restore=next(s for s in wf['jobs']['posix-4']['steps'] if s.get('name')=='Restore exact ARM stage3 compiled UA checkpoint')
    result=subprocess.run(['bash','-c',restore['run']],cwd=root,env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    protected={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in (work/'src/out/Chromix').iterdir()}
    for stage in (4,5):
        result=subprocess.run(['bash',str(root/'build/posix/ci-stage.sh'),'--platform','linux','--arch','arm64','--stage-index',str(stage),'--workdir',str(work)],cwd=root,env=env,capture_output=True,text=True)
        assert result.returncode==1 and 'build script failed' in result.stderr
    assert calls.read_text().splitlines()==['restore-exact-stage3','actual-upload-gate','continue-arm-ninja','continue-arm-ninja']
    for p,state in protected.items(): assert (p.read_bytes(),p.stat().st_mtime_ns)==state


@pytest.mark.parametrize('field,value', [('arch','x64'),('runner','ubuntu-22.04'),('linux157_ua_recovery','false'),('use_upstream_cache','true'),('resume_attempt','2')])
def test_fixed_stage4_guard_rejects_wrong_inputs(tmp_path,field,value):
    data=yaml.safe_load(arm.render());guard=next(s for s in data['jobs']['posix-4']['steps'] if s.get('name')=='Require fixed ARM stage3-to-stage4 UA continuation')
    values={'github.repository':'xiaozhou26/Chromix','github.event_name':'workflow_dispatch','github.ref':'refs/heads/main',
            'inputs.platform':'linux','inputs.arch':'arm64','inputs.runner':'ubuntu-24.04','inputs.artifact':'chromix-linux-arm64',
            'inputs.build_profile':'fast','inputs.use_upstream_cache':'false','inputs.linux157_ua_recovery':'true',
            'inputs.linux157_pre_gn_recovery':'false','inputs.linux157_post_gn_recovery':'false','inputs.resume_migration':'',
            'inputs.resume_run_id':'','inputs.resume_artifact_ids':'','inputs.resume_attempt':'1','inputs.max-stages':'8'}
    values['inputs.'+field]=value;text=guard['run']
    for key,val in values.items():text=text.replace('${{ '+key+' }}',val)
    assert '${{' not in text
    result=subprocess.run(['bash','-c',text],cwd=tmp_path,capture_output=True)
    assert result.returncode!=0 and not list(tmp_path.iterdir())


def test_native_runtime_and_fingerprint_gate_remain_nonoptional():
    gate=yaml.safe_load(arm.render())['jobs']['verify-linux-arm64']
    assert gate['runs-on']=='ubuntu-24.04-arm'
    for name,needle in [('Verify checksum and native launcher','--arch arm64 --runtime'),('Native ARM64 fingerprint regression gate','fingerprint_acceptance.py')]:
        step=next(s for s in gate['steps'] if s.get('name')==name)
        assert 'continue-on-error' not in step and needle in step['run']


def test_unreviewed_recipe_does_not_get_selector_exception():
    from tools import patch_selection
    bound=json.loads((ROOT/'build/patch-selection-157.json').read_text())['platforms']['linux']['downstream']['recipe']
    if bound != source.recipe():
        with pytest.raises(patch_selection.SelectionError, match='changed Linux157 recipe input'):
            patch_selection.select(ROOT,'linux')
    else:
        selected,raw=patch_selection.select(ROOT,'linux')
        assert len(raw)==233 and selected['selection']['downstream']['recipe']==source.recipe()
        with pytest.raises(ValueError, match='require unchanged old228 plus exact one addition'):
            ua.validate_additions(selected,raw,ua.load(ROOT,'arm64'),ROOT)
