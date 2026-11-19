"""Copied Dawn Go must use a verified stage-local Go1.27.1 standard library."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from tools import linux157_build as build


def installation(root, version=build.GO_VERSION):
    elf = b'\x7fELF\x02\x01' + b'\0' * 12 + b'\x3e\x00'
    for relative in ('bin/go', 'pkg/tool/linux_amd64/compile'):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(elf + relative.encode())
        path.chmod(0o755)
    (root / 'VERSION').write_text(version + '\n')
    (root / 'src/runtime').mkdir(parents=True)
    return root / 'bin/go'


@pytest.fixture
def host(tmp_path, monkeypatch):
    root = tmp_path / 'go'
    selected = installation(root)
    src = tmp_path / 'work/src'
    src.mkdir(parents=True)
    monkeypatch.setattr(build.toolchain.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(build.toolchain.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(build.shutil, 'which', lambda name, path=None: str(selected))
    info = {'GOROOT': str(root), 'GOVERSION': build.GO_VERSION,
            'GOHOSTOS': 'linux', 'GOHOSTARCH': 'amd64',
            'GOTOOLDIR': str(root / 'pkg/tool/linux_amd64')}
    calls = []

    def output(args, **kwargs):
        calls.append((args, kwargs))
        if 'env' in args:
            if args[0] == str(src / build.GO_TARGET):
                assert kwargs['env']['GOROOT'] == str(root)
            else:
                assert 'GOROOT' not in kwargs['env']
            return json.dumps(info)
        if '-V=full' in args:
            return 'compile version go1.27.1\n'
        return build.GO_VERSION + ' linux/amd64 ' + build.source.digest(b'chromix-go') + '\n'

    monkeypatch.setattr(build.subprocess, 'check_output', output)
    run = mock.Mock()
    monkeypatch.setattr(build.subprocess, 'run', run)
    return src, root, selected, info, calls, run


def test_copied_go_probe_binds_goroot_and_host_not_target(host):
    src, root, selected, info, calls, run = host
    env = {'PATH': str(root / 'bin'), 'GOROOT': '/stale', 'GOTOOLCHAIN': 'auto',
           'GOOS': 'windows', 'GOARCH': 'arm64', 'GOFLAGS': '-toolexec=bad'}
    identity = build.prepare_host_go(src, env)
    copied = src / build.GO_TARGET
    assert copied.is_file() and not copied.is_symlink()
    assert copied.read_bytes() == selected.read_bytes()
    assert env['GOROOT'] == str(root)
    assert env['GOARCH'] == 'amd64' and env['GOOS'] == 'linux'
    assert env['GOTOOLCHAIN'] == 'local' and env['GOENV'] == 'off'
    assert env['GOFLAGS'] == ''
    args = run.call_args.args[0]
    assert args[:2] == [str(copied), 'build']
    assert run.call_args.kwargs['env']['GOROOT'] == str(root)
    assert run.call_args.kwargs['env']['CGO_ENABLED'] == '0'
    assert identity['probe'].startswith('go1.27.1 linux/amd64 ')
    assert identity['compiler_sha256']
    # Each stage re-queries the host and compiles again even when the copy exists.
    build.prepare_host_go(src, env)
    assert run.call_count == 2
    assert sum(args[0] == str(selected) for args, _ in calls) == 2


@pytest.mark.parametrize('field,value', [('GOVERSION', 'go1.27.0'), ('GOHOSTOS', 'darwin'),
                                         ('GOHOSTARCH', 'arm64'), ('GOTOOLDIR', '/')])
def test_wrong_go_identity_rejects_before_copy_or_probe(host, field, value):
    src, _, _, info, _, run = host
    info[field] = value
    with pytest.raises(ValueError):
        build.prepare_host_go(src, {'PATH': ''})
    assert not (src / build.GO_TARGET).exists()
    run.assert_not_called()


@pytest.mark.parametrize('mutation', ['version', 'compiler-version', 'compiler-arch', 'binary-root', 'target-link', 'parent-link', 'target-bytes'])
def test_mixed_installation_and_unsafe_checkpoint_rejected(host, monkeypatch, mutation):
    src, root, selected, _, _, run = host
    target = src / build.GO_TARGET
    target.parent.mkdir(parents=True)
    if mutation == 'version':
        (root / 'VERSION').write_text('go1.26.0\n')
    elif mutation == 'compiler-version':
        original = build.subprocess.check_output
        monkeypatch.setattr(build.subprocess, 'check_output', lambda args, **kw:
                            'compile version go1.26.0' if '-V=full' in args else original(args, **kw))
    elif mutation == 'compiler-arch':
        compiler = root / 'pkg/tool/linux_amd64/compile'
        compiler.write_bytes(compiler.read_bytes()[:18] + b'\xb7\x00')
    elif mutation == 'binary-root':
        other = root.parent / 'other-go'
        other.write_bytes(selected.read_bytes() + b'changed')
        other.chmod(0o755)
        monkeypatch.setattr(build.shutil, 'which', lambda *a, **kw: str(other))
    elif mutation == 'target-link':
        target.symlink_to(selected)
    elif mutation == 'parent-link':
        target.parent.rmdir()
        target.parent.symlink_to(root / 'bin')
    else:
        target.write_bytes(b'stale binary')
    with pytest.raises(ValueError):
        build.prepare_host_go(src, {'PATH': ''})
    run.assert_not_called()


def test_probe_error_does_not_publish_environment(host, monkeypatch):
    src, _, _, _, _, _ = host
    original = build.subprocess.check_output
    monkeypatch.setattr(build.subprocess, 'check_output', lambda args, **kw:
                        'go1.26.0 linux/amd64 bad' if Path(args[0]).name == 'probe' else original(args, **kw))
    env = {'PATH': '', 'GOROOT': '/stale'}
    with pytest.raises(ValueError, match='standard-library probe'):
        build.prepare_host_go(src, env)
    assert env['GOROOT'] == '/stale'


def test_real_linux_copied_go_compiles_standard_library(tmp_path):
    if os.environ.get('CHROMIX_REAL_HOST_GO') != '1':
        pytest.skip('requires explicit Linux x64 Go1.27.1 installation')
    src = tmp_path / 'src'
    src.mkdir()
    env = dict(os.environ, GOROOT='/deliberately-invalid', GOTOOLCHAIN='auto', GOARCH='arm64')
    result = build.prepare_host_go(src, env)
    assert result['host'] == 'linux/amd64'
    assert result['probe'] == 'go1.27.1 linux/amd64 ' + build.source.digest(b'chromix-go')
    assert (src / build.GO_TARGET).is_file() and not (src / build.GO_TARGET).is_symlink()
    assert env['GOROOT'] == result['goroot']
    assert env['GOARCH'] == 'amd64'
    # Checkpoint copy is reused, but GOROOT is rebound and the actual probe reruns.
    env['GOROOT'] = '/previous-runner-root'
    assert build.prepare_host_go(src, env)['sha256'] == result['sha256']
    assert env['GOROOT'] == result['goroot']
