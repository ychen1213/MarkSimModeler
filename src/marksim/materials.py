"""Local TXT optical tables and target-frequency material evaluation.

The public format contains no Meep objects.  exp(-i*omega*t), n_complex=n+i*k,
and length units of one micrometre are used throughout the adapter.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile

import numpy as np

SCHEMA_VERSION = "1.0"
MODEL_VERSION = "txt-linear-narrowband-1.0"


def import_material_txt(path, *, wavelength_unit, columns=None, assume_lossless=False):
    """Read explicit column mapping; absent k stays unknown until authorized.

    columns is a zero-based mapping e.g. {'wavelength':0,'n':1,'k':2}.
    With columns=None exactly two or three columns are accepted. Identical
    duplicate wavelengths are collapsed; conflicting duplicates are rejected.
    """
    if type(assume_lossless) is not bool:
        raise ValueError("assume_lossless must be a boolean, not a string or number")
    path = Path(path).resolve()
    factors = {"nm": 1.0, "um": 1000.0, "µm": 1000.0, "m": 1e9}
    if wavelength_unit not in factors:
        raise ValueError("Explicit wavelength_unit must be nm, um or m")
    raw = path.read_bytes()
    rows, numbers, header = [], [], None
    for number, text in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
        text = re.split(r"#|//|;", text, maxsplit=1)[0].strip()
        if not text:
            continue
        if re.search(r",\s*,|^,|,$", text):
            raise ValueError(f"{path.name}:{number}: missing comma-delimited value")
        tokens = re.split(r"[\s,]+", text)
        try:
            row = [float(token) for token in tokens]
        except ValueError:
            if not rows and header is None and any("lambda" in t.lower() or "wavelength" in t.lower() for t in tokens):
                header = text
                continue
            raise ValueError(f"{path.name}:{number}: nonnumeric/missing material value") from None
        if not np.all(np.isfinite(row)):
            raise ValueError(f"{path.name}:{number}: nonfinite material value")
        rows.append(row)
        numbers.append(number)
    if not rows:
        raise ValueError(f"{path.name}: no numeric data")
    widths = {len(row) for row in rows}
    if len(widths) != 1:
        raise ValueError(f"{path.name}: inconsistent columns/missing values")
    width = widths.pop()
    if columns is None:
        if width not in (2, 3):
            raise ValueError("Default import requires lambda,n or lambda,n,k; provide explicit columns")
        columns = {"wavelength": 0, "n": 1, **({"k": 2} if width == 3 else {})}
    columns = dict(columns)
    if "wavelength" not in columns or "n" not in columns:
        raise ValueError("Column mapping must specify wavelength and n")
    if set(columns) - {"wavelength", "n", "k"}:
        raise ValueError("Unknown column mapping; allowed keys are wavelength, n, k")
    if len(set(columns.values())) != len(columns) or any(type(i) is not int or i < 0 or i >= width for i in columns.values()):
        raise ValueError("Column indices must be distinct valid zero-based integers")
    data = np.asarray(rows)
    with np.errstate(over="ignore", invalid="ignore"):
        wave = data[:, columns["wavelength"]] * factors[wavelength_unit]
    n = data[:, columns["n"]]
    k = data[:, columns["k"]] if "k" in columns else None
    if not np.isfinite(wave).all() or np.any(wave <= 0) or np.any(n <= 0) or (k is not None and np.any(k < 0)):
        raise ValueError("Require vacuum wavelength>0, n>0 and passive k>=0")
    order = np.argsort(wave, kind="stable")
    wave, n = wave[order], n[order]
    if k is not None:
        k = k[order]
    keep, duplicates = [], 0
    for i in range(len(wave)):
        if keep and wave[i] == wave[keep[-1]]:
            j = keep[-1]
            if n[i] != n[j] or (k is not None and k[i] != k[j]):
                raise ValueError(f"Conflicting duplicate wavelength {wave[i]:g} nm")
            duplicates += 1
        else:
            keep.append(i)
    if len(keep) < 2:
        raise ValueError("At least two distinct wavelength samples are required")
    return {
        "wavelength_nm": wave[keep].tolist(), "n": n[keep].tolist(),
        "k": None if k is None else k[keep].tolist(),
        "k_status": "unknown" if k is None else "provided",
        "assume_lossless": bool(assume_lossless), "source": str(path),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "valid_range_nm": [float(wave[0]), float(wave[-1])],
        "import": {"wavelength_unit": wavelength_unit, "columns": columns,
                   "header": header, "duplicate_identical_rows_removed": duplicates,
                   "original_numeric_rows": rows, "original_line_numbers": numbers},
    }


def import_material_folder(folder, *, wavelength_unit, assume_lossless=False, options=None):
    """Import all TXT files; per-stem options can override mapping and units."""
    files = sorted(Path(folder).glob("*.txt"))
    if not files:
        raise ValueError("Material folder contains no .txt files")
    options = options or {}
    materials = {p.stem: import_material_txt(p, **{
                "wavelength_unit": wavelength_unit, "assume_lossless": assume_lossless,
                **options.get(p.stem, {})}) for p in files}
    return {"schema_version": SCHEMA_VERSION, "model_version": MODEL_VERSION,
            "generation_method": "local_txt_import", "software_versions": {"numpy": np.__version__},
            "input_hash": hashlib.sha256(json.dumps(materials, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "materials": materials}


def evaluate_library(library, lambda_vacuum_nm):
    """Return JSON-safe target-frequency optical parameters at one target frequency.

    Interpolation is linear in n,k, never in epsilon. Extrapolation is forbidden.
    Current adapter deliberately rejects Re(epsilon)<=0 instead of changing it.
    """
    if library.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported MaterialLibrary schema_version")
    wavelength = float(lambda_vacuum_nm)
    if not np.isfinite(wavelength) or wavelength <= 0:
        raise ValueError("lambda_vacuum_nm must be finite and positive")
    result = {}
    if not library.get("materials"):
        raise ValueError("MaterialLibrary is empty")
    for name, spec in library["materials"].items():
        if type(spec.get("assume_lossless", False)) is not bool:
            raise ValueError(f"{name}: assume_lossless must be a boolean")
        wave, n_data = np.asarray(spec["wavelength_nm"], float), np.asarray(spec["n"], float)
        if wave.ndim != 1 or n_data.shape != wave.shape or len(wave) < 2:
            raise ValueError(f"{name}: invalid wavelength/n table shapes")
        if not np.all(np.isfinite(wave)) or not np.all(np.isfinite(n_data)) or np.any(wave <= 0) or np.any(n_data <= 0) or np.any(np.diff(wave) <= 0):
            raise ValueError(f"{name}: wavelengths must increase; finite positive n and wavelength required")
        if not wave[0] <= wavelength <= wave[-1]:
            raise ValueError(f"{name}: {wavelength:g} nm outside [{wave[0]:g},{wave[-1]:g}] nm; extrapolation disabled")
        warnings = []
        if spec.get("k") is None:
            if spec.get("assume_lossless") is not True:
                raise ValueError(f"{name}: k unknown; explicitly choose assume_lossless before simulation")
            k = 0.0
            warnings.append("k unknown: explicit user-selected lossless approximation")
        else:
            k_data = np.asarray(spec["k"], float)
            if k_data.shape != wave.shape or not np.all(np.isfinite(k_data)) or np.any(k_data < 0):
                raise ValueError(f"{name}: invalid/nonpassive k table")
            k = float(np.interp(wavelength, wave, k_data))
        n = float(np.interp(wavelength, wave, n_data))
        try:
            epsilon = (n + 1j * k) ** 2
        except OverflowError:
            raise ValueError(f"{name}: optical constants overflow permittivity") from None
        if not np.isfinite([epsilon.real, epsilon.imag]).all():
            raise ValueError(f"{name}: nonfinite permittivity")
        if epsilon.real <= 0:
            raise ValueError(f"{name}: Re(epsilon)<=0 unsupported by narrowband conductivity adapter; verified Drude/Lorentz required")
        if k:
            warnings.append("Target-frequency equivalent conductivity: not a broadband fit")
        result[name] = {"n": n, "k": k, "epsilon_real": float(epsilon.real),
                        "epsilon_imag": float(epsilon.imag),
                        "D_conductivity": float(2 * np.pi * (1000 / wavelength) * epsilon.imag / epsilon.real),
                        "conductivity_S_per_m": float(2*np.pi*299792458/(wavelength*1e-9)*8.8541878128e-12*epsilon.imag),
                        "model_type": "lossless_target_index" if k == 0 else "target_frequency_conductivity",
                        "lambda_vacuum_nm": wavelength, "valid_range_nm": [float(wave[0]), float(wave[-1])],
                        "model_bandwidth": "target frequency only; no broadband validity implied",
                        "source": spec.get("source"), "source_sha256": spec.get("source_sha256"),
                        "k_status": spec.get("k_status", "unknown" if spec.get("k") is None else "provided"),
                        "warnings": warnings}
    return result


def save_library(library, path):
    path = Path(path)
    if path.suffix.lower() != ".json":
        raise ValueError("Material library output must be .json; raw material TXT files are read-only inputs")
    destination = path.resolve()
    if any(spec.get("source") and Path(spec["source"]).resolve() == destination for spec in library.get("materials", {}).values()):
        raise ValueError("Refusing to overwrite an original material source")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(library, indent=2, ensure_ascii=False, allow_nan=False)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=path.name + ".", suffix=".tmp", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def load_library(path):
    library = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if library.get("schema_version") != SCHEMA_VERSION or not library.get("materials"):
        raise ValueError("Unsupported or empty MaterialLibrary")
    return library
