"""Windows image-command guardian; owns its own lock before accepting execution."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading

from . import locking
from .platform_support import docker_argv, docker_environment


def serve(lock_path, argv):
    with Path(lock_path).open("a+b") as lock:
        locking.flock(lock, locking.LOCK_EX | locking.LOCK_NB)
        sys.stdout.buffer.write(b"READY\n")
        sys.stdout.buffer.flush()
        # If the Runner died before acknowledging the lock, EOF aborts the
        # operation. After GO, the guardian keeps the lock until Docker exits.
        if sys.stdin.buffer.readline() != b"GO\n":
            return 2
        with tempfile.TemporaryFile() as source:
            while piece := sys.stdin.buffer.read(65536):
                source.write(piece)
            source.seek(0)
            return subprocess.run(argv, stdin=source, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=docker_environment()).returncode


def run(args, lock_path, source=None, timeout=900):
    process = subprocess.Popen([sys.executable, "-m", "agent_workbench.docker_worker", str(lock_path), json.dumps(args)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=docker_environment(), creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
    ready = queue.Queue()
    threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
    acknowledged = False
    try:
        try:
            value = ready.get(timeout=15)
        except queue.Empty:
            raise subprocess.TimeoutExpired("Docker guardian startup", 15) from None
        if value != b"READY\n":
            raise OSError("Docker guardian could not acquire its operation lock")
        acknowledged = True
        # Only the bounded saved Dockerfile (or its legacy tar) crosses this
        # pipe. Docker reads an explicitly selected directory on this host.
        process.communicate(b"GO\n" + (source.read() if source else b""), timeout=timeout)
        return subprocess.CompletedProcess(args, process.returncode)
    finally:
        if not acknowledged and process.poll() is None:
            process.kill()
        # A timed-out acknowledged guardian must outlive the Runner so that
        # manual acknowledgement/new operations remain blocked by its lock.
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            threading.Thread(target=lambda: (process.wait(), process.stdout.close()), daemon=True).start()
        else:
            process.wait()
            process.stdout.close()


if __name__ == "__main__":
    try:
        code = serve(sys.argv[1], docker_argv(*json.loads(sys.argv[2])))
    except Exception:
        code = 2  # Do not print paths, build output, arguments or environment.
    sys.exit(code)
