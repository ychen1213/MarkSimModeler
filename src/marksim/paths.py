"""Resolve relocated input files without rewriting scientific provenance.

Only a missing path recorded below ``source_root`` in the local relocation
manifest may be redirected. Existing paths, unrelated projects and new output
locations keep their original meaning. Task/config/HDF5 contents are untouched.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath

ROOT = Path(__file__).resolve().parents[2]


def resolve_input_path(value, *, base_dir=None):
    """Return an existing input at its original or explicitly relocated path.

    The new root is derived from this module, so moving the complete MarkSim
    folder again does not require editing the recorded destination. Windows and
    WSL spellings of the original root are accepted; no basename search occurs.
    An unavailable input is returned as-is so the normal missing-file error
    remains visible instead of selecting an unrelated file.
    """
    text = str(value)
    original = Path(value)
    windows_absolute = PureWindowsPath(text).is_absolute()
    if not original.is_absolute() and not windows_absolute and not PurePosixPath(text).is_absolute() and base_dir is not None:
        original = Path(base_dir) / original
        text = str(original)
    if original.exists():
        return original
    manifest = ROOT / "relocation_manifest.json"
    if not manifest.is_file():
        return original
    try:
        record = json.loads(manifest.read_text(encoding="utf-8-sig"))
        names = {item["name"] for item in record["moved_items"]}
        source_text = record["source_root"]
        if not isinstance(source_text, str) or not all(isinstance(n, str) for n in names):
            return original
        windows_root = PureWindowsPath(source_text)
        if windows_root.is_absolute():
            if PureWindowsPath(text).is_absolute():
                relative = PureWindowsPath(text).relative_to(windows_root)
            else:
                # Existing WSL Meep artifacts may spell the same disk as /mnt/d.
                if len(windows_root.drive) != 2 or windows_root.drive[1] != ":":
                    return original
                wsl_root = PurePosixPath("/mnt", windows_root.drive[0].lower(), *windows_root.parts[1:])
                relative = PurePosixPath(text).relative_to(wsl_root)
            allowed = {name.casefold() for name in names}
            matches = bool(relative.parts) and relative.parts[0].casefold() in allowed
        else:
            relative = PurePosixPath(text).relative_to(PurePosixPath(source_text))
            matches = bool(relative.parts) and relative.parts[0] in names
        if not matches or ".." in relative.parts:
            return original
        destination = ROOT.joinpath(*relative.parts).resolve()
        if destination.is_relative_to(ROOT.resolve()) and destination.exists():
            return destination
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # A malformed/missing migration record cannot make another file valid.
        pass
    return original
