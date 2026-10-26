"""Bounded CI tests, with isolated parallel modules on Windows."""
import faulthandler
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import subprocess
import sys
import unittest


class TimedResult(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        faulthandler.dump_traceback_later(90, exit=True)

    def stopTest(self, test):
        faulthandler.cancel_dump_traceback_later()
        super().stopTest(test)


def run_module(name):
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--module", name],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        return name, result.returncode, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return name, 1, "Module exceeded 10 minutes; its process was stopped.\n"


def main():
    if sys.argv[1:2] != ["--module"] and (os.name == "nt" or "--parallel" in sys.argv):
        # Git/SQLite fixtures are filesystem intensive on Windows. Modules use
        # separate processes and private temp directories, with no shared mocks.
        modules = sorted(path.stem for path in Path("tests").glob("test_*.py"))
        failed = False
        with ThreadPoolExecutor(max_workers=4) as pool:
            for future in as_completed([pool.submit(run_module, name) for name in modules]):
                name, code, output = future.result()
                print(f"\n{name}: {'PASSED' if code == 0 else 'FAILED'}", flush=True)
                print(output, end="", flush=True)
                failed |= code != 0
        return int(failed)
    if sys.argv[1:2] == ["--module"]:
        sys.path.insert(0, str(Path("tests").resolve()))
        suite = unittest.defaultTestLoader.loadTestsFromName(sys.argv[2])
    else:
        suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult, failfast=True).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
