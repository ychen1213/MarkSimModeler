"""Versioned, JSON-compatible contracts. Geometry uses micrometres, not nm."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import numpy as np

SCHEMA_VERSION = "1.0"
MODEL_VERSION = "marksim-fdtd-0.1.0"
PHYS_TO_MEEP_2D = np.array([[1., 0., 0.], [0., 0., 1.], [0., -1., 0.]])

def jsonable(value):
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return jsonable(value.tolist())
    if isinstance(value, np.generic):
        return jsonable(value.item())
    if isinstance(value, complex):
        return [value.real, value.imag]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value

def canonical_hash(value):
    return hashlib.sha256(json.dumps(jsonable(value), sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()

def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def read_json(path):
    # Project .yaml files deliberately use the JSON subset of YAML 1.2.
    with Path(path).open(encoding="utf-8-sig") as f:
        return json.load(f)

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(jsonable(value), indent=2, ensure_ascii=False,
                                    allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)

def versioned(value, kind=None):
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Unsupported {kind or 'object'} schema_version; expected {SCHEMA_VERSION}")
    return value

def finite_scalar(value, name, minimum=None, strict=False):
    x = float(value)
    if not np.isfinite(x) or (minimum is not None and (x <= minimum if strict else x < minimum)):
        raise ValueError(f"{name}: invalid finite range")
    return x

def validate_geometry(g):
    versioned(g, "GeometryModel")
    if g.get("units") != "um":
        raise ValueError("GeometryModel must explicitly use units='um'")
    if g.get("representation") not in ("parametric_solids", "voxel_labels"):
        raise ValueError("Unsupported geometry representation")
    bounds = np.asarray(g["cell_bounds_um"], float)
    if bounds.shape != (3, 2) or not np.all(np.isfinite(bounds)) or np.any(bounds[:, 1] <= bounds[:, 0]):
        raise ValueError("cell_bounds_um must contain three increasing intervals")
    if g["representation"] == "parametric_solids":
        finite_scalar(g["period_x_um"], "period_x_um", 0, True)
        if not np.isclose(bounds[0, 1] - bounds[0, 0], g["period_x_um"]):
            raise ValueError("Cell x width must equal explicitly specified parent period")
        for s in g["solids"]:
            p = np.asarray(s["polygon_xy_um"], float)
            if p.ndim != 2 or p.shape[1] != 2 or len(p) < 3 or not np.all(np.isfinite(p)):
                raise ValueError("Invalid solid polygon")
            if not np.isfinite([s["z_min_um"], s["z_max_um"]]).all() or s["z_min_um"] >= s["z_max_um"]:
                raise ValueError("Solid z interval must be finite and increasing")
            if not s.get("material"):
                raise ValueError("Solid has no material")
    return g

SIMULATION_DEFAULTS = {
    "schema_version": SCHEMA_VERSION, "model_version": MODEL_VERSION,
    "backend": "periodic_2d", "lambda_vacuum_nm": 643.0,
    "resolution_per_um": 24.0, "pml_um": 1.0,
    "z_bounds_um": [-2.5, 3.0], "source_z_um": 1.6,
    "reflection_monitor_z_um": 1.0, "transmission_monitor_z_um": -0.7,
    "phase_reference_z_um": 0.8, "phase_origin_um": [0., 0., 0.],
    "fwidth_fraction": 0.12, "after_source_time": 60., "tail_time": 20.,
    "dft_relative_tolerance": 0.001, "weak_amplitude_threshold": 1e-5,
    "max_wall_seconds": 600., "max_grid_cells": 3000000,
    "incident_direction": [0., 0., -1.], "polarizations": ["s", "p"],
    "resources": 1,
    "time_unit": "um/c",
}

def simulation_config(config):
    unknown=set(config)-set(SIMULATION_DEFAULTS)-{'period_x_um','notes','model_version'}
    if unknown:
        raise ValueError(f"Unknown SimulationConfig fields (not executed): {sorted(unknown)}")
    s = copy.deepcopy(SIMULATION_DEFAULTS)
    s.update(config)
    versioned(s, "SimulationConfig")
    if s["backend"] != "periodic_2d":
        raise ValueError("Native FDTD supports periodic_2d only; 3D geometry preview does not imply 3D solving")
    if s["incident_direction"] != [0., 0., -1.] or s["polarizations"] != ["s", "p"]:
        raise ValueError("MVP requires normal incidence from +z and both s/p runs")
    if s["resources"] != 1:
        raise ValueError("This runner is validated for one process only")
    if s['time_unit']!='um/c':
        raise ValueError('FDTD time parameters use um/c; max_wall_seconds alone is wall-clock seconds')
    for name in ("lambda_vacuum_nm", "resolution_per_um", "pml_um", "fwidth_fraction",
                 "after_source_time", "tail_time", "max_wall_seconds", "max_grid_cells",
                 "dft_relative_tolerance", "weak_amplitude_threshold"):
        s[name] = finite_scalar(s[name], name, 0, True)
    if s["fwidth_fraction"] > 0.3:
        raise ValueError("Narrowband material adapter requires fwidth_fraction <=0.3")
    a, b = np.asarray(s["z_bounds_um"], float)
    if not np.isfinite([a, b]).all() or b - a <= 2 * s["pml_um"]:
        raise ValueError("Invalid z_bounds_um/PML")
    for key in ("source_z_um", "reflection_monitor_z_um", "transmission_monitor_z_um", "phase_reference_z_um"):
        z = finite_scalar(s[key], key)
        if not a + s["pml_um"] < z < b - s["pml_um"]:
            raise ValueError(f"{key} must be outside PML")
    if not s["transmission_monitor_z_um"] < s["reflection_monitor_z_um"] < s["source_z_um"]:
        raise ValueError("Required order: transmission monitor < reflection monitor < source")
    origin = np.asarray(s["phase_origin_um"], float)
    if origin.shape != (3,) or not np.isfinite(origin).all():
        raise ValueError("phase_origin_um must be a finite 3-vector")
    if not np.allclose(origin[1:],0,atol=1e-15):
        raise ValueError('MVP phase_origin_um is lateral x only (y=z=0); use phase_reference_z_um for height phase')
    return s
