"""Explicit always-on web+worker launcher; not enabled by autoscale deployment.

An operator must approve an always-on runtime before using start:with-worker in
production. Either process failing stops its sibling so supervision can restart
the whole unit. No migrations or provider initialization happen here.
"""
import os
import signal
import subprocess
import sys
import time


def commands():
    return [
        [sys.executable, "-m", "gunicorn", "--bind", f"0.0.0.0:{os.getenv('PORT', '8080')}",
         "--workers", "2", "--threads", "2", "--timeout", "180", "sceneit.server:app"],
        [sys.executable, "-m", "sceneit.import_worker"],
    ]


def supervise(process_commands=None):
    children = []
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for command in process_commands or commands():
            children.append(subprocess.Popen(command))
        while not stopping:
            if any(child.poll() is not None for child in children):
                return 1
            time.sleep(.25)
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(supervise())