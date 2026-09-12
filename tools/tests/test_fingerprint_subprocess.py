"""Execute only owned fixture processes; never kill by executable name."""
from pathlib import Path
import os
import sys
import time

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fingerprint_subprocess import run_command


def invoke(tmp_path, source, timeout=5, env=None):
    return run_command([sys.executable, '-X', 'utf8', '-c', source], timeout=timeout,
                       cwd=tmp_path, log=tmp_path / 'process.log', env=env)


@pytest.mark.parametrize('code', [0, 3])
def test_exit_and_combined_output(tmp_path, code):
    result = invoke(tmp_path, f'import sys; print("out"); print("err", file=sys.stderr); sys.exit({code})')
    assert result['exit_code'] == code and not result['timed_out']
    assert not result['cleanup_errors']
    assert set((tmp_path / 'process.log').read_text().split()) == {'out', 'err'}


@pytest.mark.parametrize('wait_for_timeout', [False, True])
def test_owned_detached_descendant_cleanup(tmp_path, wait_for_timeout):
    pid_file = tmp_path / 'child.pid'
    child_source = 'import os,time; from pathlib import Path; Path("child.pid").write_text(str(os.getpid())); time.sleep(60)'
    source = f'''
import subprocess, sys, os, time
from pathlib import Path
options = {{'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}} if os.name == 'nt' else {{'start_new_session': True}}
child = subprocess.Popen([sys.executable, '-c', {child_source!r}], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
deadline = time.monotonic() + 5
while not Path('child.pid').exists() and time.monotonic() < deadline:
    time.sleep(0.02)
time.sleep({60 if wait_for_timeout else 0.3})
'''
    try:
        result = invoke(tmp_path, source, timeout=2 if wait_for_timeout else 8)
        assert pid_file.exists(), result
        pid = int(pid_file.read_text())
        assert result['timed_out'] is wait_for_timeout
        assert result['exit_code'] == (None if wait_for_timeout else 0)
        assert not result['cleanup_errors'], result
        deadline = time.monotonic() + 2
        while True:
            try:
                if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.NoSuchProcess:
                break
            # Windows job accounting reaches zero before the process disappears
            # from every system enumeration; allow that bounded teardown delay.
            assert time.monotonic() < deadline, f'owned child still running: {pid}'
            time.sleep(0.02)
    finally:
        # A broken implementation must not leave this test's sleeper running.
        if pid_file.exists():
            try:
                child = psutil.Process(int(pid_file.read_text()))
                if child.create_time() >= time.time() - 30:
                    child.kill()
                    try:
                        child.wait(timeout=3)
                    except psutil.TimeoutExpired:
                        pass  # nonexecuting orphan zombies on some containers
            except psutil.NoSuchProcess:
                pass


def test_timeout_is_bounded(tmp_path):
    result = invoke(tmp_path, 'import time; time.sleep(60)', timeout=0.3)
    assert result['timed_out'] and result['exit_code'] is None
    assert result['duration_seconds'] < 12 and not result['cleanup_errors']


def test_no_clobber_and_invalid_bound(tmp_path):
    log = tmp_path / 'process.log'; log.write_bytes(b'keep')
    with pytest.raises(FileExistsError):
        invoke(tmp_path, 'print("unexpected")')
    assert log.read_bytes() == b'keep'
    for bound in (0, -1, 7201):
        with pytest.raises(ValueError, match='timeout'):
            invoke(tmp_path, '', timeout=bound)


def test_cwd_and_environment(tmp_path):
    result = invoke(tmp_path, 'import os; print(os.environ["CHROMIX_TEST_VALUE"]); print(os.getcwd())',
                    env={**os.environ, 'CHROMIX_TEST_VALUE': 'isolated'})
    assert result['exit_code'] == 0
    lines = (tmp_path / 'process.log').read_text().splitlines()
    assert lines == ['isolated', str(tmp_path)]
