"""Use MarkSim's read-only migration map when this legacy app is bundled."""
from pathlib import Path
import sys


def resolve_input_path(value, *, base_dir=None):
    root = Path(__file__).resolve().parents[2]
    source = root / "src"
    if (source / "marksim" / "paths.py").is_file():
        if str(source) not in sys.path:
            sys.path.insert(0, str(source))
        from marksim.paths import resolve_input_path as resolve
        return resolve(value, base_dir=base_dir)
    path = Path(value)
    return Path(base_dir) / path if base_dir is not None and not path.is_absolute() else path
