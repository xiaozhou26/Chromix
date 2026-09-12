"""Bounded audit subprocesses with owned-descendant cleanup (never kill by name)."""
from __future__ import annotations
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


class WindowsJob:
    """Gate the child until it belongs to a kill-on-close Windows Job Object."""
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        class Basic(c.Structure):
            _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64),
                        ('flags', w.DWORD), ('min_ws', c.c_size_t), ('max_ws', c.c_size_t),
                        ('active', w.DWORD), ('affinity', c.c_size_t),
                        ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in ('read_ops', 'write_ops', 'other_ops',
                        'read_bytes', 'write_bytes', 'other_bytes')]
        class Extended(c.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', c.c_size_t),
                        ('job_memory', c.c_size_t), ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]
        class Accounting(c.Structure):
            _fields_ = [(name, c.c_int64) for name in ('user', 'kernel', 'period_user', 'period_kernel')] + [
                (name, w.DWORD) for name in ('faults', 'total', 'active', 'terminated')]
        self.Accounting = Accounting
        self.c = c
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        for name, args, result in (
                ('CreateJobObjectW', [c.c_void_p, w.LPCWSTR], w.HANDLE),
                ('SetInformationJobObject', [w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
                ('QueryInformationJobObject', [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p], w.BOOL),
                ('AssignProcessToJobObject', [w.HANDLE, w.HANDLE], w.BOOL),
                ('TerminateJobObject', [w.HANDLE, w.UINT], w.BOOL),
                ('CloseHandle', [w.HANDLE], w.BOOL)):
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, c.byref(info), c.sizeof(info)):
            self.close()
            raise c.WinError(c.get_last_error())

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise self.c.WinError(self.c.get_last_error())

    def terminate(self):
        if not self.kernel.TerminateJobObject(self.handle, 124):
            raise self.c.WinError(self.c.get_last_error())
        deadline = time.monotonic() + 10
        while True:
            info = self.Accounting()
            if not self.kernel.QueryInformationJobObject(self.handle, 1, self.c.byref(info), self.c.sizeof(info), None):
                raise self.c.WinError(self.c.get_last_error())
            if info.active == 0:
                return
            if time.monotonic() >= deadline:
                raise OSError('Windows audit job still has active descendants after termination')
            time.sleep(0.05)

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.kernel.CloseHandle(handle):
                raise self.c.WinError(self.c.get_last_error())


def run_command(command, *, timeout, log, cwd, env=None):
    """Return exit/timing evidence; a timeout or incomplete cleanup is not a pass."""
    import psutil
    if not 0 < timeout <= 7200:
        raise ValueError('subprocess timeout must be in (0, 7200]')
    started = time.monotonic()
    process, job, parent, descendants = None, None, None, {}
    result = {'command': [str(v) for v in command], 'timed_out': False,
              'exit_code': None, 'cleanup_errors': []}
    try:
        with Path(log).open('xb') as output:
            options = {'cwd': cwd, 'env': env, 'stdout': output, 'stderr': subprocess.STDOUT}
            if os.name == 'nt':
                job = WindowsJob()
                gate = ('import subprocess,sys; '
                        'sys.exit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL) '
                        'if sys.stdin.buffer.read(1)==b"1" else 125)')
                process = subprocess.Popen([sys.executable, '-c', gate, *command],
                    stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW, **options)
                job.assign(process)
                process.stdin.write(b'1'); process.stdin.close()
            else:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True, **options)
            try:
                parent = psutil.Process(process.pid)
            except psutil.NoSuchProcess:
                # Very short POSIX commands can finish before psutil attaches.
                pass
            while True:
                if not job:
                    # Playwright may create a detached browser process; process
                    # groups alone do not cover it. Retain PID+creation identity.
                    for owner in ([parent] if parent else []) + list(descendants.values()):
                        try:
                            for child in owner.children(recursive=True):
                                descendants[(child.pid, child.create_time())] = child
                        except psutil.NoSuchProcess:
                            pass
                if process.poll() is not None:
                    result['exit_code'] = process.returncode
                    break
                if time.monotonic() - started >= timeout:
                    result['timed_out'] = True
                    break
                time.sleep(0.05)
    finally:
        if job:
            try:
                job.terminate()
            except OSError as error:
                result['cleanup_errors'].append(str(error))
            try:
                job.close()
            except OSError as error:
                result['cleanup_errors'].append(str(error))
        else:
            # Cover short-lived, nondetached children between psutil samples too.
            # Only signal the session we created, while a known member still
            # belongs to it; never target an unrelated process by executable name.
            if process and os.name != 'nt':
                own_group = process.poll() is None
                for child in descendants.values():
                    try:
                        if child.is_running() and os.getpgid(child.pid) == process.pid:
                            own_group = True
                    except (ProcessLookupError, psutil.NoSuchProcess):
                        pass
                if own_group:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except OSError as error:
                        result['cleanup_errors'].append(str(error))
            for child in reversed(list(descendants.values())):
                try:
                    child.kill()  # psutil guards against PID reuse.
                except psutil.NoSuchProcess:
                    pass
                except psutil.Error as error:
                    result['cleanup_errors'].append(str(error))
            _, alive = psutil.wait_procs(list(descendants.values()), timeout=5)
            for child in alive:
                # POSIX orphan zombies may await init reaping but cannot execute.
                try:
                    if child.status() != psutil.STATUS_ZOMBIE:
                        result['cleanup_errors'].append('descendant still alive: ' + str(child.pid))
                except psutil.NoSuchProcess:
                    pass
        if process:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                result['cleanup_errors'].append('audit process did not exit after termination')
    result['duration_seconds'] = round(time.monotonic() - started, 3)
    return result
