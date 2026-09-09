"""Cancellable process groups with independent deadline/parent-death guards."""
import os
import signal
import subprocess
import sys


def start_guarded(arguments, *, timeout, **options):
    return subprocess.Popen(
        [sys.executable, "-m", "sceneit.process_guard", str(os.getpid()), str(timeout), *arguments],
        start_new_session=True, **options)


def kill_and_wait(child):
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def bounded_process(arguments, *, timeout):
    """No caller releases a permit before its whole tool group is terminated."""
    child = start_guarded(arguments, timeout=timeout, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
    try:
        return child.wait(timeout=timeout)
    except BaseException:
        kill_and_wait(child)
        raise