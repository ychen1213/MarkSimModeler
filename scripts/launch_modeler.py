"""Launch with editable local interpreter selection; no implicit installation."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def select_interpreter():
    settings = ROOT / "config" / "local_runtime.json"
    if settings.exists():
        config = json.loads(settings.read_text(encoding="utf-8-sig"))
        python = Path(os.path.expandvars(config["python"])).expanduser()
        if not python.is_absolute():
            python = ROOT / python
    else:
        local = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        python = local if local.is_file() else Path(sys.executable)
    if not python.is_file():
        raise FileNotFoundError(f"Configured Python does not exist: {python}; edit config/local_runtime.json or run setup.bat.")
    return python.resolve()


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description="MarkSimModeler source launcher")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--smoke-test", action="store_true", help="Create and close the real Tk window to verify desktop startup")
    parser.add_argument("--project")
    args = parser.parse_args()
    python = select_interpreter()
    if args.check_only:
        return subprocess.call([str(python), "-X", "utf8", str(ROOT / "scripts" / "check_environment.py")], cwd=ROOT)
    if args.smoke_test:
        return subprocess.call([str(python), "-X", "utf8", str(ROOT / "scripts" / "smoke_gui.py")], cwd=ROOT)
    # Verify dependencies in the selected interpreter, not the launcher's one.
    check = subprocess.run([str(python), "-X", "utf8", "-c", "import tkinter,numpy,scipy,h5py,matplotlib,PIL,gdstk,plotly"], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if check.returncode:
        print("Selected Python lacks required modules. Run setup.bat, or install requirements.txt in your chosen environment.", file=sys.stderr)
        print(check.stderr, file=sys.stderr)
        return check.returncode
    command = [str(python), "-X", "utf8", str(ROOT / "run_modeler.py"), "gui"]
    if args.project:
        command += ["--project", args.project]
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
