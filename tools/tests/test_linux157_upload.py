"""Execute generated failed-checkpoint packing, identity, stream and upload gates."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from unittest import mock

import pytest
import yaml

from tools import verify_linux157_upload as upload
from tools.tests import test_platform_workflows as workflows

ROOT = Path(__file__).resolve().parents[2]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    work = root / 'chromix-build'
    src = work / 'src'
    write(src / 'value.cc', 'current source\n')
    write(src / 'chrome/VERSION', 'MAJOR=157\nMINOR=0\nBUILD=8103\nPATCH=78\n')
    write(src / '.chromix-source-ready', 'exact-ready')
    write(src / upload.source.RECEIPT, '{}')
    for name in ('ungoogled-chromium', 'ungoogled-chromium-portablelinux'):
        write(work / 'tooling' / name / '.git/HEAD', name + '\n')
    write(work / 'tooling/ungoogled-chromium-portablelinux/patches/series', '')
    inputs = {'series_sha256': 'a' * 64, 'patches': [{'path': 'fixture.patch', 'sha256': 'b' * 64}]}
    expected = {'value.cc': hashlib.sha256(b'current source\n').hexdigest()}
    report = {'schema_version': 1, 'status': 'verified', 'method': 'reverse-forward-in-scratch',
              'identity': inputs, 'patch_count': 1, 'outputs': expected}
    write(work / 'fingerprint-diagnostics/source-final.json', json.dumps(report))
    write(src / 'out/Chromix/args.gn', 'target_cpu = "x64"\nv8_target_cpu = "x64"\n')
    write(src / 'out/Chromix/build.ninja', 'rule good\n  command = echo object > latest.o\n'
          'rule bad\n  command = echo intentional-compile-failure; exit 7\n'
          'build latest.o: good\nbuild chrome: bad latest.o\n')
    (root / 'chromix-logs').mkdir()
    manifest = {'core_inputs': {}, 'platform_inputs': {}, 'toolchain_inputs': {}, 'patches': [],
                'upstream_series_sha256': hashlib.sha256(b'').hexdigest()}
    receipt = {'prepared_outputs': expected}
    monkeypatch.setattr(upload.source, 'checkpoint', lambda *a: {'status': 'verified', 'fixture_only': True})
    monkeypatch.setattr(upload.source, 'verify_receipt', lambda *a: receipt)
    monkeypatch.setattr(upload.source, 'load_manifest', lambda *a: manifest)
    monkeypatch.setattr(upload.verify_patch_stack, 'load_stack', lambda *a, **k: (
        inputs, [('fixture.patch', b'', [('value.cc', 'modify', None)])]))
    # The real host Ninja is probed/executed; only Linux architecture discovery is
    # simulated when this workflow test runs on the macOS development host.
    monkeypatch.setattr(upload.restore_ninja, 'host_identity', lambda: ('linux', 'x64'))
    monkeypatch.setattr(upload.restore_ninja, 'binary_architectures', lambda *a: {'x64'})
    return root, work


def run_ninja(work):
    result = subprocess.run(['ninja', '-C', str(work / 'src/out/Chromix'), 'chrome'], capture_output=True)
    assert result.returncode != 0
    assert (work / 'src/out/Chromix/latest.o').read_text() == 'object\n'
    return result


@pytest.mark.parametrize('mutation', ['source', 'source-final', 'version', 'gn-arch', 'ninja-log', 'ninja-deps', 'provenance', 'unprepared'])
def test_live_validation_rejects_changed_identity(tree, mutation, monkeypatch):
    _, work = tree
    run_ninja(work)
    assert upload.validate(work, 'x64')['ninja']['sha256']
    if mutation == 'source':
        (work / 'src/value.cc').write_text('stale\n')
    elif mutation == 'source-final':
        path = work / 'fingerprint-diagnostics/source-final.json'
        value = json.loads(path.read_text()); value['identity']['series_sha256'] = 'c' * 64
        path.write_text(json.dumps(value))
    elif mutation == 'version':
        (work / 'src/chrome/VERSION').write_text('MAJOR=156\nMINOR=0\nBUILD=8078\nPATCH=79\n')
    elif mutation == 'gn-arch':
        (work / 'src/out/Chromix/args.gn').write_text('target_cpu = "arm64"\n')
    elif mutation == 'ninja-log':
        (work / 'src/out/Chromix/.ninja_log').write_text('# ninja log v99\n')
    elif mutation == 'ninja-deps':
        (work / 'src/out/Chromix/.ninja_deps').write_bytes(b'wrong')
    else:
        monkeypatch.setattr(upload.source, 'checkpoint', mock.Mock(side_effect=ValueError(mutation)))
    with pytest.raises(ValueError):
        upload.validate(work, 'x64')


def test_pre_gn_valid_source_needs_no_fabricated_ninja_or_source_final(tree):
    _, work = tree
    shutil.rmtree(work / 'src/out')
    (work / 'fingerprint-diagnostics/source-final.json').unlink()
    result = upload.validate(work, 'x64')
    assert result['ninja'] is None and result['source_final'] is None
    assert result['runtime_verified'] is False


def render(script):
    return script.replace("${{ inputs.arch }}", 'x64').replace("${{ inputs.platform }}", 'linux')


@pytest.mark.parametrize('stage', [1, 8])
@pytest.mark.parametrize('mutation', ['none', 'unprepared', 'changed-live', 'corrupt-archive', 'different-packed-source', 'different-packed-git', 'different-packed-ninja', 'different-packed-source-final'])
def test_generated_failure_pack_verify_and_upload_admission(tree, stage, mutation, pre_gn=False):
    root, work = tree
    workflow = yaml.safe_load((ROOT / '.github/workflows/build-posix-github.yml').read_text())
    steps = workflow['jobs'][f'posix-{stage}']['steps']
    preserve = next(s for s in steps if s.get('id') == 'linux_runtime_checkpoint')
    verify = next(s for s in steps if s.get('id') == 'checkpoint')
    uploads = [s for s in steps if s.get('name', '').startswith('Upload tree part')]
    if pre_gn:
        shutil.rmtree(work / 'src/out')
        (work / 'fingerprint-diagnostics/source-final.json').unlink()
    else:
        run_ninja(work)
    # Materialize the real validator result; the generated subprocess adapter only
    # supplies this locally verified admission to isolate GitHub runner wiring.
    admission = upload.validate(work, 'x64')
    write(root / 'admission.json', json.dumps(admission))
    write(root / 'tools/linux157_source.py', '''import os,sys
from pathlib import Path
assert sys.argv[1:3] == ['--phase','checkpoint']
if os.environ.get('INVALID_CHECKPOINT')=='1': raise SystemExit(23)
print('{"status":"verified","fixture_only":true}')
''')
    write(root / 'tools/verify_linux_checkpoint.py', "raise SystemExit('wrong legacy ready-key route')\n")
    write(root / 'tools/verify_linux157_upload.py', f'''import sys,json,hashlib
from pathlib import Path
sys.path.insert(0, {str(ROOT / 'tools')!r})
from verify_linux157_upload import verify_archive
if '--archive-report' in sys.argv:
    verify_archive(sys.stdin.buffer,json.loads(Path(sys.argv[-1]).read_text()))
else:
    report=json.loads(Path('admission.json').read_text())
    root=Path({str(work)!r})
    for name,value in report['files'].items():
        p=root/name
        if value is None:
            assert not p.exists(),name
        else:
            assert p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==value,name
    print(json.dumps(report))
''')
    for relative in ('build/posix/ci-parts.sh', 'tools/snapshot_volumes.py', 'tools/verify_snapshot_tar.py'):
        path = root / relative; path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, path)
    env = dict(os.environ, RUNNER_TEMP=str(root), GITHUB_OUTPUT=str(root / 'outputs'),
               CHROMIUM_VERSION='157.0.8103.78', COPYFILE_DISABLE='1', CHROMIX_SNAPSHOT_VOLUME_BYTES='65536')
    if mutation == 'unprepared': env['INVALID_CHECKPOINT'] = '1'
    packed = subprocess.run(['bash', '-c', render(preserve['run'])], cwd=root, env=env, capture_output=True, text=True)
    snapshot = work / f'.snapshot-stage-{stage}'
    if mutation == 'unprepared':
        assert packed.returncode == 23
        assert not snapshot.exists()
        checked = subprocess.run(['bash', '-c', render(verify['run'])], cwd=root, env=env, capture_output=True, text=True)
        assert checked.returncode == 23
    else:
        assert packed.returncode == 0, packed.stderr
        assert (root / 'outputs').read_text() == 'upload_snapshot=true\n'
        if mutation == 'changed-live':
            (work / 'src/value.cc').write_text('changed after packing\n')
        elif mutation == 'corrupt-archive':
            last = sorted(snapshot.rglob('tree.tar.zst.*'))[-1]
            last.write_bytes(last.read_bytes()[:-7])
        elif mutation.startswith('different-packed-'):
            target = {'different-packed-source': work / 'src/value.cc',
                      'different-packed-git': work / 'tooling/ungoogled-chromium/.git/HEAD',
                      'different-packed-ninja': work / 'src/out/Chromix/build.ninja',
                      'different-packed-source-final': work / 'fingerprint-diagnostics/source-final.json'}[mutation]
            original = target.read_bytes(); target.write_bytes(original + b'changed\n')
            subprocess.run(['bash', str(ROOT / 'build/posix/ci-parts.sh'), str(work), str(snapshot)], env=env, check=True, capture_output=True)
            target.write_bytes(original)
        checked = subprocess.run(['bash', '-c', render(verify['run'])], cwd=root, env=env, capture_output=True, text=True)
        assert (checked.returncode == 0) == (mutation == 'none'), checked.stdout + checked.stderr
    evaluator = workflows.LinuxFailedCheckpointTest()
    for step in uploads:
        allowed = evaluator.allowed(step, **{'linux_runtime_checkpoint.upload_snapshot': 'true',
                                             'checkpoint_outcome': 'success' if checked.returncode == 0 else 'failure'})
        assert allowed == (mutation == 'none')
    assert admission['runtime_verified'] is False


def test_pre_gn_tool_failure_tree_reaches_generated_upload_gate(tree):
    test_generated_failure_pack_verify_and_upload_admission(tree, 1, 'none', pre_gn=True)


def test_generated_all_stages_match_and_native_gates_unchanged():
    path = ROOT / '.github/workflows/build-posix-github.yml'
    before = path.read_bytes()
    subprocess.run([sys.executable, str(ROOT / 'tools/gen_posix_workflow.py')], cwd=ROOT, check=True)
    assert path.read_bytes() == before
    workflow = yaml.safe_load(before)
    original = yaml.safe_load(subprocess.check_output(['git', '-C', str(ROOT), 'show', 'HEAD:.github/workflows/build-posix-github.yml']))
    assert workflow['jobs']['verify-linux-arm64'] == original['jobs']['verify-linux-arm64']
    for stage in range(1, 9):
        new = workflow['jobs'][f'posix-{stage}']['steps']; old = original['jobs'][f'posix-{stage}']['steps']
        assert len(new) == len(old)
        for a, b in zip(new, old):
            if a.get('id') == 'checkpoint':
                assert a['if'] == b['if']
            else:
                assert a == b
