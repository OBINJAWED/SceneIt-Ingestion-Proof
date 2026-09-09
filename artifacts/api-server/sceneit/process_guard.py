"""Internal Linux process-group watchdog, independent of its caller's survival."""
import os
import signal
import subprocess
import sys
import threading
import time


def guard(parent_pid, timeout, arguments):
    # The caller starts this wrapper as a new session/process-group leader.
    # Keep it alive beside the actual tool: an exec would discard the watchdog.
    deadline = time.monotonic() + timeout
    stopped = threading.Event()

    def watchdog():
        while not stopped.wait(0.1):
            if os.getppid() != parent_pid or time.monotonic() >= deadline:
                os.killpg(os.getpgrp(), signal.SIGKILL)

    if os.getppid() != parent_pid:
        return 125
    threading.Thread(target=watchdog, name="sceneit-process-guard", daemon=True).start()
    try:
        # stdin/stdout may be pipes for private JSON IPC, never log their contents.
        return subprocess.call(arguments)
    finally:
        stopped.set()


if __name__ == "__main__":
    raise SystemExit(guard(int(sys.argv[1]), float(sys.argv[2]), sys.argv[3:]))