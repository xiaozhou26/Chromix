"""Exact latest ICU-compiled donor migration through the one-file0238 addition."""
from __future__ import annotations

import copy
import difflib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from unittest import mock

import pytest
import yaml

from tools import linux157_ua as ua
from tools import linux157_source as source
from tools import gen_linux157_ua_workflow as workflow
from tools.tests.test_linux157_pre_gn_workflow import put

ROOT=source.ROOT


def make_patch(path, before, after):
    raw=(f'diff --git a/{path} b/{path}\n'+''.join(difflib.unified_diff(
        before.splitlines(True),after.splitlines(True),fromfile='a/'+path,tofile='b/'+path))).encode()
    data,entries=ua.arp.transform_patch(raw,set(),[])
    return path+'.patch',data,entries


def stack_fixture(root):
    src=root/'src';src.mkdir();stack=[]
    for i in range(227):
        name=f'old/{i}.cc';p=src/name;p.parent.mkdir(exist_ok=True);p.write_text(f'patched{i}\n')
        stack.append(make_patch(name,f'original{i}\n',p.read_text()))
    path=ua.TARGETS[0];p=src/path;p.parent.mkdir(parents=True);p.write_text('#include "base/system/sys_info.h"\n')
    stack.append(make_patch(path,'old source\n',p.read_text()))
    stack.append(make_patch(path,p.read_text(),p.read_text()+'#include "base/version.h"\n'))
    return src,stack


def test_exact_latest_donor_includes_icu_fix_and_compiled_progress():
    m=ua.load();e=ua.expected('x64')
    assert (e['run_id'],e['job_id'],e['head_sha'],e['artifacts'][0]['id']) == (
        44630942999,135937091377,'b14a0160001d35ed5de83c59521d2e35383cd4eb',14584551831)
    assert e['artifacts'][0]['digest']=='sha256:e4120c00740d549f4111f959f3ad0e6b85d0d2ff3cf78c790ec65f75372b3d5b'
    assert m['source_final']['patch_count']==228
    assert [Path(p['path']).name[:5] for p in m['source_receipt']['chromix_selection']['patches'][-2:]]==['0236-','0237-']
    assert m['files']['src/out/Chromix/.ninja_log'] and m['files']['src/out/Chromix/.ninja_deps']
    assert m['files']['src/.chromix-linux157-locale-migration.json']
    assert m['migration']['new_patch_count']==229
    assert [p['target'] for p in m['migration']['additions']]==['components/embedder_support/user_agent_utils.cc']
    assert ua.expected('arm64')['run_id'] == 44639133090
    assert ua.expected('arm64')['stage'] == 3


@pytest.mark.parametrize('field',['run_id','attempt','stage','job_id','head_sha','arch'])
def test_wrong_donor_metadata_rejects_before_io(tmp_path,field):
    m=ua.expected('x64');m[field]='wrong' if field in ('head_sha','arch') else m[field]+1
    with pytest.raises(ValueError,match='tuple'):ua.admit(tmp_path,'x64',m)
    assert not list(tmp_path.iterdir())


def test_complete228_and229_private_roundtrip_only_one_source(tmp_path):
    src,stack=stack_fixture(tmp_path);before_out=ua.snapshot_outputs(src)
    before,revised=ua.private_migration(src,stack,tmp_path)
    assert len(stack)==229 and len(before)==228
    assert revised[ua.TARGETS[0]].endswith(b'#include "base/version.h"\n')
    assert ua.snapshot_outputs(src)==before_out
    for name,(raw,_) in before.items():assert (src/name).read_bytes()==raw


def test_extra_target_or_old_source_tampering_fails_closed(tmp_path):
    src,stack=stack_fixture(tmp_path)
    stack[-1]=make_patch('old/3.cc','patched3\n','extra edit\n')
    with pytest.raises(ValueError,match='exact ordered'):ua.private_migration(src,stack,tmp_path)
    assert (src/'old/3.cc').read_text()=='patched3\n'


def test_migration_recipe_cannot_change_existing_locale_identity():
    from tools import linux157_storage
    old=ua.load()['source_receipt']['recipe']
    current=linux157_storage.load()['source_receipt']['recipe']
    ua.recipe_delta(old,current)
    with pytest.raises(ValueError):ua.recipe_delta(old,source.recipe())
    bad=copy.deepcopy(current);bad['inputs']['tools/linux157_locale.py']='0'*64
    with pytest.raises(ValueError,match='inherited'):ua.recipe_delta(old,bad)
    bad=copy.deepcopy(current);bad['inputs']['build/linux/157.0.8103.78/locale-checkpoint.json']='0'*64
    with pytest.raises(ValueError):ua.recipe_delta(old,bad)


def metadata_client(attack=None):
    audit=os.environ.get('CHROMIX_UA_AUDIT')
    if not audit:pytest.skip('requires exact latest cloud metadata')
    root=Path(audit);run=json.loads((root/'run.json').read_text());jobs=json.loads((root/'jobs.json').read_text())['jobs'];artifacts=json.loads((root/'artifacts.json').read_text())['artifacts']
    if attack=='sha':next(a for a in artifacts if a['id']==14584551831)['digest']='sha256:'+'0'*64
    elif attack=='attempt':run['run_attempt']=2
    elif attack=='head':run['head_sha']='0'*40
    elif attack=='job':next(j for j in jobs if j['id']==135937091377)['id']=1
    class C:
        def get(self,path):return run
        def items(self,path,key):return jobs if key=='jobs' else artifacts
    return C()


def test_real_metadata_accepts_latest_only():
    assert ua.validate_metadata(metadata_client(),'x64')==ua.expected('x64')


@pytest.mark.parametrize('attack',['sha','attempt','head','job'])
def test_real_metadata_mutation_rejected(attack):
    with pytest.raises(ValueError):ua.validate_metadata(metadata_client(attack),'x64')


def test_dedicated_workflow_is_exact_and_keeps_failure_preservation():
    text=workflow.render();assert text==(ROOT/workflow.OUT).read_text();data=yaml.safe_load(text)
    for stage in range(1,9):
        steps=data['jobs'][f'posix-{stage}']['steps'];run=next(s for s in steps if s.get('id')=='stage')
        assert 'CHROMIX_LINUX157_UA_RECOVERY' in run['env'] and 'CHROMIX_LINUX157_LOCALE_RECOVERY' not in run['env']
        assert any(s.get('name')=='Preserve latest failed Linux source/output checkpoint' for s in steps)
    restore=next(s for s in data['jobs']['posix-1']['steps'] if s.get('name')=='Restore latest compiled Linux157 UA checkpoint')
    assert 'linux157_ua.py --phase restore' in restore['run'] and 'linux157_locale.py' not in restore['run']
    caller=yaml.safe_load((ROOT/'.github/workflows/build-linux157-x64-ua-resume.yml').read_text())
    assert caller['concurrency']['cancel-in-progress'] is False
    event=caller.get('on',caller.get(True));assert set(event['workflow_dispatch']['inputs'])=={'compile_jobs'}


@pytest.mark.parametrize('failure,code,commands', [
    ('none', 0, ['update', 'install']),
    ('update-error', 100, ['update']),
    ('install-error', 100, ['update', 'install']),
    ('update-timeout', 124, ['update']),
    ('install-timeout', 124, ['update', 'install']),
])
def test_generated_apt_commands_fail_closed(tmp_path, failure, code, commands):
    data = yaml.safe_load(workflow.render())
    steps = data['jobs']['posix-1']['steps']
    dependencies = next(s for s in steps if s.get('name') == 'Install Linux build dependencies')
    stage = next(s for s in steps if s.get('id') == 'stage')
    assert dependencies['timeout-minutes'] == 20
    assert 'continue-on-error' not in dependencies
    assert 'if' not in stage  # The build retains GitHub Actions' default success() gate.
    assert steps.index(dependencies) < steps.index(stage)
    for number in range(2, 9):
        job = data['jobs'][f'posix-{number}']
        assert f"needs.posix-{number - 1}.result == 'success'" in job['if']
        assert next(s for s in job['steps'] if s.get('name') == dependencies['name']) == dependencies

    real_timeout = shutil.which('timeout') or shutil.which('gtimeout')
    assert real_timeout, 'GNU timeout is required for executable timeout coverage'
    put(tmp_path / 'bin/sudo', '#!/bin/sh\nexec "$@"\n')
    # Keep the generated timeout arguments checked; only shorten the test clock.
    put(tmp_path / 'bin/timeout', f'''#!/bin/bash
set -euo pipefail
test "$1:$2" = '-k:30s'
case "$3" in 300s|600s) ;; *) exit 91 ;; esac
printf '%s\\n' "$3" >> "$TIMEOUT_LOG"
shift 3
exec {shlex.quote(real_timeout)} -k 1s 1s "$@"
''')
    put(tmp_path / 'bin/apt-get', f'''#!{sys.executable}
import json, os, sys, time
args = sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
command = 'update' if 'update' in args else 'install'
if os.environ['FAILURE'] == command + '-timeout':
    time.sleep(60)
if os.environ['FAILURE'] == command + '-error':
    raise SystemExit(100)
''')
    put(tmp_path / 'build/posix/ci-stage.sh', '#!/bin/sh\nprintf "build\\n" >> "$BUILD_LOG"\n')
    (tmp_path / 'chromix-logs').mkdir()
    env = dict(os.environ, PATH=str(tmp_path / 'bin') + os.pathsep + os.environ['PATH'],
               CALL_LOG=str(tmp_path / 'calls'), BUILD_LOG=str(tmp_path / 'build-calls'),
               TIMEOUT_LOG=str(tmp_path / 'timeouts'), FAILURE=failure,
               RUNNER_TEMP=str(tmp_path), CHROMIX_JOB_START_EPOCH='0')
    build_run = stage['run']
    for key, value in [('inputs.platform', 'linux'), ('inputs.arch', 'x64'), ("inputs['max-stages']", '8')]:
        build_run = build_run.replace('${{ ' + key + ' }}', value)
    assert '${{' not in build_run
    result = subprocess.run(['bash', '-c', dependencies['run'] + '\n' + build_run],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == code, result.stderr
    assert (tmp_path / 'build-calls').exists() == (code == 0)
    calls = [json.loads(line) for line in (tmp_path / 'calls').read_text().splitlines()]
    assert len(calls) == len(commands)
    options = ['-o', 'Acquire::http::Timeout=30', '-o', 'Acquire::https::Timeout=30',
               '-o', 'Acquire::Retries=3', '-o', 'DPkg::Lock::Timeout=60']
    for args, command in zip(calls, commands):
        assert args[:len(options)] == options
        assert args[len(options)] == command
        if command == 'update':
            assert args[len(options) + 1:] == ['-o', 'APT::Update::Error-Mode=any']
        else:
            assert args[len(options) + 1] == '-y'
            assert not any(arg.startswith('-') for arg in args[len(options) + 2:])
    assert (tmp_path / 'timeouts').read_text().splitlines() == [
        '300s' if command == 'update' else '600s' for command in commands]


@pytest.mark.parametrize('arch,locale_optin,expected',[('x64','0',True),('x64','1',False),('arm64','0',False)])
def test_ci_ua_optin_is_exclusive_and_x64_only(tmp_path,arch,locale_optin,expected):
    work=tmp_path/'work';(work/'src/out/Chromix').mkdir(parents=True);(work/'src/out/Chromix/keep.o').write_bytes(b'ICU compiled')
    put(tmp_path/'CHROMIUM_LINUX_VERSION','157.0.8103.78\n')
    put(tmp_path/'build/posix/ci-stage.sh',(ROOT/'build/posix/ci-stage.sh').read_text())
    put(tmp_path/'tools/linux157_source.py',"print('{}')\n")
    put(tmp_path/'tools/linux157_build.py',"from pathlib import Path\nimport os\nPath(os.environ['CALL_LOG']).write_text('continue latest')\nraise SystemExit(17)\n")
    put(tmp_path/'bin/timeout','#!/bin/sh\nshift 5\nexec "$@"\n')
    env=dict(os.environ,PATH=str(tmp_path/'bin')+os.pathsep+os.environ['PATH'],CALL_LOG=str(tmp_path/'calls'),CHROMIX_USE_UPSTREAM_CACHE='0',CHROMIX_BUILD_PROFILE='fast',CHROMIX_LINUX157_UA_RECOVERY='1',CHROMIX_LINUX157_LOCALE_RECOVERY=locale_optin,CHROMIX_LINUX157_PRE_GN_RECOVERY='0',CHROMIX_LINUX157_POST_GN_RECOVERY='0')
    result=subprocess.run(['bash',str(tmp_path/'build/posix/ci-stage.sh'),'--platform','linux','--arch',arch,'--workdir',str(work)],cwd=tmp_path,env=env,capture_output=True)
    assert result.returncode!=0 and (tmp_path/'calls').exists()==expected
    assert (work/'src/out/Chromix/keep.o').read_bytes()==b'ICU compiled'


def test_real_latest229_private_replay_preserves_icu_and_all_objects(tmp_path):
    donor=os.environ.get('CHROMIX_UA_COMPILED_SRC');patchrepo=os.environ.get('CHROMIX_UA_PATCH_REPO')
    if not donor or not patchrepo:pytest.skip('requires verified latest compiled donor and actual0238 patch-agent source')
    src=Path(donor);work=src.parent;m=ua.load();ua.prior.verify_files(work,m['files'])
    ua.prior.verify_files(src,m['source_receipt']['substituted_outputs'])
    core=work/'tooling/ungoogled-chromium';listed=set((core/'domain_substitution.list').read_text().splitlines());rules=ua.arp._rules((core/'domain_regex.list').read_bytes());stack=[]
    additions={p['patch'] for p in m['migration']['additions']}
    entries=m['source_receipt']['chromix_selection']['patches']+[{'path':p['patch'],'sha256':p['sha256']} for p in m['migration']['additions']]
    for e in entries:
        repo=Path(patchrepo) if e['path'] in additions else ROOT
        raw=source.regular(repo,e['path']).read_bytes();assert source.digest(raw)==e['sha256']
        data,targets=ua.arp.transform_patch(raw,listed,rules);stack.append((e['path'],data,targets))
    protected=ua.snapshot_outputs(src/'out');before,after=ua.private_migration(src,stack,tmp_path)
    for pin in m['migration']['additions']:
        assert source.digest(before[pin['target']][0])==pin['before']
        assert source.digest(after[pin['target']])==pin['after']
        assert (src/pin['target']).read_bytes()==before[pin['target']][0]
    assert ua.snapshot_outputs(src/'out')==protected


def test_exact0238_cannot_change_locale_or_other_sources(tmp_path):
    m=copy.deepcopy(ua.load());old=m['source_receipt']['chromix_selection'];selected=copy.deepcopy(old)
    # Only the new raw addition is synthetic in this negative surface test.
    path='base/i18n/icubridge/default_icu_locale.h';raw=make_patch(path,'old\n','new\n')[1]
    pin=m['migration']['additions'][0];pin['sha256']=source.digest(raw)
    selected['patches'].append({'path':pin['patch'],'sha256':pin['sha256']})
    selected['selection']['additions']['patches'].append({'path':pin['patch'],'sha256':pin['sha256']})
    with pytest.raises(ValueError,match='approved source'):
        ua.validate_additions(selected,[('old',b'')]*228+[(pin['patch'],raw)],m)


def test_both_blockers_required_for_single_source_publication(tmp_path):
    src,stack=stack_fixture(tmp_path);before,revised=ua.private_migration(src,stack,tmp_path)
    with pytest.raises(ValueError,match='both transaction'):ua.publish_sources(src,before,revised)
    for name in (ua.PENDING,'.chromix-layer-in-progress'):(src/name).touch()
    original=ua.atomic_bytes
    with mock.patch.object(ua,'atomic_bytes',side_effect=OSError('interrupted')):
        with pytest.raises(OSError):ua.publish_sources(src,before,revised)
    assert (src/ua.TARGETS[0]).read_bytes()==before[ua.TARGETS[0]][0]
    with pytest.raises(ValueError,match='incomplete'):ua.verify(tmp_path,'x64')


def test_generated_restore_preserves_icu_receipts_then_continues_ninja(tmp_path):
    root=tmp_path.resolve();work=root/'chromix-build';(root/'chromix-logs').mkdir();calls=root/'calls'
    put(root/'CHROMIUM_LINUX_VERSION','157.0.8103.78\n')
    put(root/'build/posix/ci-stage.sh',(ROOT/'build/posix/ci-stage.sh').read_text())
    put(root/'bin/timeout','#!/bin/sh\nshift 5\nexec "$@"\n')
    put(root/'tools/linux157_ua.py',"""import os,sys,json
from pathlib import Path
a=sys.argv;w=Path(a[a.index('--workdir')+1]);assert not w.exists();assert a[a.index('--arch')+1]=='x64'
(w/'src/out/Chromix').mkdir(parents=True);(w/'fingerprint-diagnostics').mkdir()
for n,v in [('args.gn','PGO=0;target=x64'),('build.ninja','ICU compiled graph'),('keep.o','ICU compiled object'),('.ninja_log','# ninja log v7')]:
 (w/'src/out/Chromix'/n).write_text(v)
(w/'src/.chromix-linux157-locale-migration.json').write_text('protected old locale proof')
(w/'src/.chromix-linux157-ua-migration.json').write_text('{\"count\":229,\"run\":44630942999}')
(w/'fingerprint-diagnostics/source-final.json').write_text('{\"patch_count\":229}')
with open(os.environ['CALL_LOG'],'a') as f:f.write('restore-latest228-to229\\n')
""")
    put(root/'tools/linux157_source.py',"""import os,sys,json
from pathlib import Path
a=sys.argv
if a[a.index('--phase')+1]=='checkpoint':
 w=Path(a[a.index('--workdir')+1]);assert json.loads((w/'src/.chromix-linux157-ua-migration.json').read_text())['count']==229
print('{}')
""")
    put(root/'tools/verify_linux157_upload.py',"""import os,sys,json
from pathlib import Path
w=Path(sys.argv[sys.argv.index('--workdir')+1]);assert json.loads((w/'fingerprint-diagnostics/source-final.json').read_text())['patch_count']==229
with open(os.environ['CALL_LOG'],'a') as f:f.write('source-upload-gate\\n')
""")
    put(root/'tools/linux157_build.py',"""import os,sys
from pathlib import Path
w=Path(sys.argv[sys.argv.index('--workdir')+1]);assert (w/'src/out/Chromix/keep.o').read_text()=='ICU compiled object'
assert (w/'src/.chromix-linux157-locale-migration.json').read_text()=='protected old locale proof'
with open(os.environ['CALL_LOG'],'a') as f:f.write('continue-ninja\\n')
raise SystemExit(17)
""")
    env=dict(os.environ,RUNNER_TEMP=str(root),GITHUB_WORKSPACE=str(root),CALL_LOG=str(calls),PATH=str(root/'bin')+os.pathsep+os.environ['PATH'],CHROMIX_USE_UPSTREAM_CACHE='0',CHROMIX_BUILD_PROFILE='fast',CHROMIX_LINUX157_UA_RECOVERY='1',CHROMIX_LINUX157_LOCALE_RECOVERY='0',CHROMIX_LINUX157_PRE_GN_RECOVERY='0',CHROMIX_LINUX157_POST_GN_RECOVERY='0')
    data=yaml.safe_load(workflow.render());step=next(s for s in data['jobs']['posix-1']['steps'] if s.get('name')=='Restore latest compiled Linux157 UA checkpoint')
    result=subprocess.run(['bash','-c',step['run']],cwd=root,env=env,capture_output=True,text=True);assert result.returncode==0,result.stderr
    protected={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in (work/'src/out/Chromix').iterdir()}
    for stage in (1,2):
        result=subprocess.run(['bash',str(root/'build/posix/ci-stage.sh'),'--platform','linux','--arch','x64','--stage-index',str(stage),'--workdir',str(work)],cwd=root,env=env,capture_output=True,text=True)
        assert result.returncode==1 and 'build script failed' in result.stderr
    assert calls.read_text().splitlines()==['restore-latest228-to229','source-upload-gate','continue-ninja','continue-ninja']
    for p,state in protected.items():assert (p.read_bytes(),p.stat().st_mtime_ns)==state


@pytest.mark.parametrize('mutation',['old226','newhash','rawbytes','rawpath'])
def test_old228_prefix_and_exact0238_identity_are_not_optional(mutation):
    m=copy.deepcopy(ua.load());selected=copy.deepcopy(m['source_receipt']['chromix_selection']);pin=m['migration']['additions'][0]
    # Use actual reviewed patch bytes only when the separate patch agent is provided.
    patchrepo=os.environ.get('CHROMIX_UA_PATCH_REPO')
    if not patchrepo:pytest.skip('requires actual0238 patch-agent bytes')
    data=(Path(patchrepo)/pin['patch']).read_bytes();assert source.digest(data)==pin['sha256']
    raw=[(e['path'],source.regular(ROOT,e['path']).read_bytes()) for e in selected['patches']]
    selected['patches'].append({'path':pin['patch'],'sha256':pin['sha256']});selected['selection']['additions']['patches'].append(selected['patches'][-1]);raw.append((pin['patch'],data))
    selected['series_sha256']=ua.canonical_series(ROOT,selected,(ROOT/'patches/series').read_bytes())
    if mutation=='old226':selected['patches'].pop(225)
    elif mutation=='newhash':selected['patches'][-1]['sha256']='0'*64
    elif mutation=='rawbytes':raw[-1]=(raw[-1][0],raw[-1][1]+b'unreviewed')
    else:raw[-1]=('patches/0238-wrong.patch',raw[-1][1])
    with pytest.raises(ValueError):ua.validate_additions(selected,raw,m)
