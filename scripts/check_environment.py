"""Read-only dependency/import check; does not claim any geometry or solver test."""
from __future__ import annotations
import importlib
import json
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    modules = {}
    for name in ("numpy", "scipy", "h5py", "matplotlib", "PIL", "gdstk", "plotly", "tkinter", "markmodel.ui"):
        module = importlib.import_module(name)
        modules[name] = getattr(module, "__version__", "import OK")
    print(json.dumps({"python": platform.python_version(), "executable": sys.executable, "dependencies": modules,
                      "solver_required": False, "scope": "dependency imports only"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
