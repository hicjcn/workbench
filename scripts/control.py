"""Standard-library bootstrap for Windows CMD/PowerShell and local virtualenvs."""
import argparse
import ctypes
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_workbench.configuration import Configuration, ConfigurationError, config_path, launcher_directory


@contextmanager
def exclusive(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        api.CreateFileW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.CreateFileW(str(path), 0xC0000000, 0, None, 4, 0x80, None)
        if handle == ctypes.c_void_p(-1).value: raise ConfigurationError("API/Runner may still be using this data directory. Stop them before setup.")
        try: yield
        finally: api.CloseHandle(handle)
    else:
        import fcntl
        with path.open("a+b") as handle:
            try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise ConfigurationError("Stop API/Runner before setup.") from None
            yield


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["setup", "start", "stop", "status", "config"])
    parser.add_argument("--config")
    parser.add_argument("--skip-image-build", action="store_true")
    parser.add_argument("--rebuild-image", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    os.environ.setdefault("PYTHONUTF8", "1")
    selected = config_path(args.config)
    explicit = args.config is not None or bool(os.environ.get("AWB_CONFIG_FILE"))
    if args.action in ("stop", "status"):
        record = launcher_directory(selected) / "instance.json"
        if not record.exists():
            print(json.dumps({"phase": "STOPPED", "managed": False})); return 0
        # Use the running instance even after its source config was edited.
        metadata = json.loads(record.read_text(encoding="utf-8"))
        python = Path(metadata["python_executable"])
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT / "src")}
        return subprocess.run([str(python), "-m", "agent_workbench.launcher", args.action, "--config", str(selected) if selected else "-"], cwd=ROOT, env=environment).returncode
    if args.action == "setup" and selected and not selected.exists() and not explicit:
        with os.fdopen(os.open(selected, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
            output.write((ROOT / "workbench.example.toml").read_bytes())
        print("Created workbench.toml. Review it before starting.", flush=True)
    configuration = Configuration.load(args.config)
    configuration.apply()
    python = configuration.venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    environment = {**os.environ, "PYTHONUTF8": "1"}
    environment["PYTHONPATH"] = str(ROOT / "src") + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
    def run(argv):
        result = subprocess.run(argv, cwd=ROOT, env=environment)
        if result.returncode: raise SystemExit(result.returncode)
    if args.action == "config":
        print(json.dumps(configuration.public(), ensure_ascii=False)); return 0
    if args.action == "setup":
        # Import lifecycle dependencies only after installing them; the initial
        # ownership probe below uses only stdlib and existing kernel lock handles.
        record_dir = (configuration.file.parent if configuration.file else Path.cwd()) / ".workbench-run"
        if record_dir.exists():
            for record in record_dir.glob("*/instance.json"):
                data = json.loads(record.read_text(encoding="utf-8"))
                if data.get("phase") not in ("STOPPED", "FAILED", "NEEDS_ATTENTION"):
                    raise ConfigurationError("Stop managed instances before upgrading dependencies.")
        with ExitStack() as setup_locks:
            setup_locks.enter_context(exclusive(launcher_directory(selected) / "operation.lock"))
            setup_locks.enter_context(exclusive(configuration.data_dir / "locks/controller.lock"))
            if configuration.venv_dir.exists() and any(configuration.venv_dir.iterdir()) and not (configuration.venv_dir / "pyvenv.cfg").is_file():
                raise ConfigurationError("venv_dir is not a virtual environment; refusing to overwrite it.")
            if not python.exists(): run([sys.executable, "-m", "venv", str(configuration.venv_dir)])
            run([str(python), "-m", "pip", "install", "-e", ".[server]"])
            npm = "npm.cmd" if os.name == "nt" else "npm"
            run([npm, "ci", "--prefix", "web", "--no-audit", "--no-fund"])
            run([npm, "run", "--prefix", "web", "build"])
        if not args.skip_image_build:
            run([str(python), "-m", "agent_workbench.launcher", "build-image", "--config", str(configuration.file) if configuration.file else "-", *(["--rebuild"] if args.rebuild_image else [])])
        print("Setup complete. Run start.cmd or scripts/start.ps1."); return 0
    if not python.is_file(): raise ConfigurationError("Virtual environment is missing. Run setup.cmd first.")
    argv = [str(python), "-m", "agent_workbench.launcher", args.action, "--config", str(selected) if selected else "-"]
    if args.no_browser: argv.append("--no-browser")
    return subprocess.run(argv, cwd=ROOT, env=environment).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ConfigurationError as error:
        print(str(error), file=sys.stderr); sys.exit(1)
    except Exception:
        print("Local script failed. Check configuration, Python/Node/Docker availability and local logs. No credentials were printed.", file=sys.stderr); sys.exit(1)
