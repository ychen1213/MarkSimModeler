"""Create only a project-local environment. Existing environments are reused."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="Install editable MarkSimModeler dependencies without administrator privileges")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if sys.version_info[:2] not in ((3, 11), (3, 12)) or sys.maxsize <= 2**32:
        raise SystemExit("Use 64-bit Python 3.11 or 3.12, including tkinter, venv and pip.")
    try:
        import tkinter  # noqa: F401
    except ImportError:
        raise SystemExit("This Python lacks tkinter. Use a Python distribution with Tcl/Tk support.")
    target = ROOT / ".venv"
    python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if target.exists() and (not python.is_file() or not (target / "pyvenv.cfg").is_file()):
        raise SystemExit("Existing .venv is incomplete; it was not replaced. Select or repair the environment explicitly.")
    if target.is_symlink():
        raise SystemExit("Refusing to install into a .venv symlink outside the project.")
    if args.check_only:
        print(f"Python {sys.version.split()[0]} x64; tkinter available; target: {target}")
        return
    if not target.exists():
        venv.EnvBuilder(with_pip=True).create(target)
    # Never invoke a global pip or request elevation. Binary-only avoids a
    # compiler/admin prerequisite on ordinary company Windows installations.
    subprocess.run([str(python), "-m", "pip", "install", "--only-binary=:all:", "-r", str(ROOT / "requirements.txt")], check=True)
    subprocess.run([str(python), str(ROOT / "scripts" / "check_environment.py")], check=True)
    print("Ready. Run the launcher or: .venv\\Scripts\\python.exe run_modeler.py gui")


if __name__ == "__main__":
    main()
