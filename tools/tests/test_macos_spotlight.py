"""Check Spotlight state on the build data volume without host modification."""
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX shell contract')


def script():
    workflow = yaml.safe_load((ROOT/'.github/workflows/build-posix-github.yml').read_text())
    scripts = [step['run'] for job in workflow['jobs'].values()
               for step in job.get('steps', []) if step.get('name') == 'Disable Spotlight indexing']
    assert len(scripts) == 8
    assert len(set(scripts)) == 1
    return scripts[0]


def run_case(tmp_path, statuses, disable_code=0, query_code=0):
    config = tmp_path/'config.json'
    config.write_text(json.dumps({'statuses': statuses, 'disable': disable_code, 'query': query_code}))
    mock = tmp_path/'mdutil.py'
    mock.write_text('''import json,sys
from pathlib import Path
root=Path(sys.argv[1]); args=sys.argv[2:]
with (root/'calls').open('a') as f:f.write(' '.join(args)+'\\n')
cfg=json.loads((root/'config.json').read_text())
assert args[0]=='mdutil' and args[-1]=='/System/Volumes/Data'
if args[1]=='-i':sys.exit(cfg['disable'])
assert args[1]=='-s'
p=root/'count'; n=int(p.read_text()) if p.exists() else 0;p.write_text(str(n+1))
print('/System/Volumes/Data:\\n    '+cfg['statuses'][min(n,len(cfg['statuses'])-1)])
sys.exit(cfg['query'])
''')
    import shlex, sys
    prefix = ('sudo() { '+shlex.quote(sys.executable)+' '+shlex.quote(str(mock))+' '
              +shlex.quote(str(tmp_path))+' "$@"; }; sleep() { :; };\n')
    result = subprocess.run(['/bin/bash', '-c', prefix+script()], text=True, capture_output=True, timeout=15)
    return result, (tmp_path/'calls').read_text().splitlines()


def test_disabled_succeeds(tmp_path):
    result, calls = run_case(tmp_path, ['Indexing disabled.'])
    assert result.returncode == 0
    assert len(calls) == 2


def test_transition_retries_and_verifies(tmp_path):
    result, calls = run_case(tmp_path, ['kMDConfigSearchLevelTransitioning', 'Indexing disabled.'], 1)
    assert result.returncode == 0
    assert len(calls) == 4


@pytest.mark.parametrize('status', ['Indexing enabled.', 'kMDConfigSearchLevelTransitioning', 'not Indexing disabled.'])
def test_unverified_state_fails(tmp_path, status):
    result, calls = run_case(tmp_path, [status])
    assert result.returncode != 0
    assert len(calls) == 6
    assert '::error::' in result.stdout


def test_failed_query_cannot_pass_by_text(tmp_path):
    result, _ = run_case(tmp_path, ['Indexing disabled.'], query_code=1)
    assert result.returncode != 0


def test_generator_matches_workflow():
    subprocess.run(['python3', str(ROOT/'tools/gen_posix_workflow.py'), '--check'], cwd=ROOT, check=True)
