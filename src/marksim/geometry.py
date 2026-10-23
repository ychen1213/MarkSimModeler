"""GDS -> deterministic vertical geometric process -> disjoint solids (um).

These operations are geometric rules, not lithography/etch/deposition physics.
gdstk is imported lazily: reading generated geometry needs only JSON/numpy.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import tempfile

import numpy as np
from .paths import resolve_input_path

SCHEMA_VERSION = "1.0"
MODEL_VERSION = "gds-vertical-csg-1.1"
PRECISION_UM = 1e-8


def _gds():
    import gdstk
    return gdstk


def _bool(value, name):
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean, not a string or number")
    return value


def _finite(value, name):
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{name} must be a finite number") from None
    if not np.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return value


def _polygon(points):
    points = np.asarray(points, float)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 3 or not np.isfinite(points).all():
        raise ValueError("Mask polygon must contain at least three finite xy points")
    return _gds().Polygon(points)


def _rect(bounds):
    bounds = np.asarray(bounds, float)
    if bounds.ndim != 2 or bounds.shape[1] != 2 or bounds.shape[0] < 2 or not np.isfinite(bounds).all() or np.any(bounds[:, 1] <= bounds[:, 0]):
        raise ValueError("Rectangle/ROI bounds must be finite increasing intervals")
    return _gds().rectangle((bounds[0][0], bounds[1][0]), (bounds[0][1], bounds[1][1]))


def _boolean(a, b, op):
    if not a:
        return []
    return _gds().boolean(a, b, op, precision=PRECISION_UM)


def _simple(polygons):
    """Vertical decomposition removes GDS keyhole bridges before Prism export.

    Splitting at every vertex x means no open strip can enclose a hole. Each
    result is a hole-free trapezoid or triangle; exact material area is kept.
    """
    out = []
    for polygon in polygons:
        xs = np.unique(np.round(polygon.points[:, 0] / PRECISION_UM) * PRECISION_UM)
        pieces = _gds().slice(polygon, xs[1:-1], "x", precision=PRECISION_UM) if len(xs) > 2 else [[polygon]]
        for group in pieces:
            for piece in group:
                if piece.area() > PRECISION_UM ** 2:
                    out.append(piece)
    return out


def inspect_gds(path):
    """Read true GDS unit/precision, cells and layer/datatype selections."""
    path = resolve_input_path(path).resolve()
    library = _gds().read_gds(path)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "unit_m": library.unit, "precision_m": library.precision,
            "top_cells": [c.name for c in library.top_level()],
            "cells": [{"name": c.name, "layers_datatypes": sorted({(p.layer, p.datatype) for p in c.get_polygons()})}
                      for c in library.cells]}


def read_gds_polygons(path, *, cell, layer, datatype, roi_um=None):
    """Resolve hierarchy, arrays, mirrors and rotations, preserving source."""
    if type(layer) is not int or type(datatype) is not int or layer < 0 or datatype < 0:
        raise ValueError("GDS layer and datatype must be nonnegative integers")
    roi = None if roi_um is None else _rect(roi_um)
    path = resolve_input_path(path).resolve()
    library = _gds().read_gds(path)
    cells = {c.name: c for c in library.cells}
    if cell not in cells:
        raise ValueError(f"GDS cell {cell!r} not found; choose one of {list(cells)}")
    polygons = cells[cell].get_polygons(apply_repetitions=True, include_paths=True,
                                       depth=None, layer=int(layer), datatype=int(datatype))
    factor = library.unit / 1e-6
    polygons = [p.copy().scale(factor) for p in polygons]
    if roi is not None:
        polygons = _boolean(polygons, [roi], "and")
    return polygons, {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "cell": cell, "layer": int(layer), "datatype": int(datatype),
                      "gds_unit_m": library.unit, "gds_precision_m": library.precision,
                      "scale_to_um": factor, "roi_um": roi_um}


def wrap_polygons(polygons, bounds, period_x_um, *, period_y_um=None):
    """Periodic translates are unioned and clipped, including crossing seams."""
    period_x_um = _finite(period_x_um, "period_x_um")
    if period_x_um <= 0:
        raise ValueError("period_x_um must be positive")
    clip = _rect(bounds)
    if period_y_um is not None:
        period_y_um = _finite(period_y_um, "period_y_um")
        if period_y_um <= 0 or not np.isclose(bounds[1][1] - bounds[1][0], period_y_um, atol=1e-9, rtol=0):
            raise ValueError("period_y_um must be positive and equal cell y width")
    xmin, xmax = bounds[0]
    ymin, ymax = bounds[1]
    if not np.isclose(xmax - xmin, period_x_um, atol=1e-9, rtol=0):
        raise ValueError("Explicit mother period must equal x cell width")
    shifted = []
    for poly in polygons:
        if not np.isfinite(poly.points).all():
            raise ValueError("Periodic polygon contains nonfinite coordinates")
        bb = poly.bounding_box()
        ix0 = math.ceil((xmin - bb[1][0]) / period_x_um)
        ix1 = math.floor((xmax - bb[0][0]) / period_x_um)
        ys = [0] if period_y_um is None else range(math.ceil((ymin - bb[1][1]) / period_y_um), math.floor((ymax - bb[0][1]) / period_y_um) + 1)
        if (ix1 - ix0 + 1) * len(ys) > 10000:
            raise ValueError("GDS extent would require too many periodic copies; select a smaller ROI")
        for ix in range(ix0, ix1 + 1):
            for iy in ys:
                shifted.append(poly.copy().translate(ix * period_x_um, 0 if period_y_um is None else iy * period_y_um))
    return _simple(_boolean(shifted, [clip], "and"))


def _mask(step, recipe, base_dir):
    bounds = recipe["cell_bounds_um"]
    bias = _finite(step.get("cd_bias_um", 0), "cd_bias_um")
    angle = math.radians(_finite(step.get("rotation_deg", 0), "rotation_deg"))
    dx, dy = _finite(step.get("delta_x_um", 0), "delta_x_um"), _finite(step.get("delta_y_um", 0), "delta_y_um")
    if step.get("mask") is None:
        polygons, source = [_rect(bounds)], {"kind": "full_cell"}
    else:
        mask = step["mask"]
        if "polygons_xy_um" in mask:
            polygons = [_polygon(p) for p in mask["polygons_xy_um"]]
            source = {"kind": "explicit_polygons"}
        else:
            path = resolve_input_path(mask["gds_path"], base_dir=base_dir)
            polygons, source = read_gds_polygons(path, cell=mask["cell"], layer=mask["layer"],
                                                datatype=mask.get("datatype", 0), roi_um=mask.get("roi_um"))
        if not polygons:
            selection = f"cell={mask.get('cell')}, layer={mask.get('layer')}, datatype={mask.get('datatype', 0)}, ROI={mask.get('roi_um')}" if "gds_path" in mask else "explicit polygons"
            raise ValueError(f"{step['id']}: mask selection contains no polygons ({selection}); check the selected layer/ROI or disable this step")
        if bias:
            # CD bias is total width increase: each side moves bias/2.
            polygons = _gds().offset(polygons, bias / 2, join="miter", precision=PRECISION_UM, use_union=True)
        polygons = [p.copy().rotate(angle).translate(dx, dy) for p in polygons]
        polygons = wrap_polygons(polygons, bounds, recipe["period_x_um"], period_y_um=recipe.get("period_y_um"))
        if not polygons:
            raise ValueError(f"{step['id']}: mask became empty after CD/transform/cell clipping; check bias, position and cell bounds or disable this step")
    if step.get("mask_polarity", "positive") == "negative":
        polygons = _simple(_boolean([_rect(bounds)], polygons, "not"))
    elif step.get("mask_polarity", "positive") != "positive":
        raise ValueError("mask_polarity must be positive or negative")
    return polygons, source


def invariance_evidence(geometry):
    """Explain reduction independently from the user's invariant_y declaration."""
    y_bounds = np.asarray(geometry["cell_bounds_um"], float)[1]
    rejected = []
    for solid in geometry["solids"]:
        p = np.asarray(solid["polygon_xy_um"], float)
        lo, hi = p.min(axis=0), p.max(axis=0)
        area = abs(np.dot(p[:, 0], np.roll(p[:, 1], 1)) - np.dot(p[:, 1], np.roll(p[:, 0], 1))) / 2
        rectangular = np.isclose(area, np.prod(hi - lo), atol=1e-8, rtol=1e-8)
        full_y = np.allclose([lo[1], hi[1]], y_bounds, atol=1e-8, rtol=0)
        if not (rectangular and full_y):
            rejected.append({"name": solid["name"], "rectangular_xy": bool(rectangular), "spans_full_y": bool(full_y)})
    return {"declared_invariant_y": geometry["invariant_y"], "verified_full_y_rectangles": not rejected,
            "periodic_2d_eligible": bool(geometry["invariant_y"] and not rejected),
            "test": "Every Boolean-partitioned solid is a rectangle covering the full declared y interval; 90-degree vertical extrusion",
            "failed_solids": rejected,
            "interpretation": "The GDS cell/ROI is assumed to repeat along y only when declared; this does not prove a finite physical mark is infinitely long."}


def _rewrite(solids, mask, z0, z1, *, material=None, targets=None, name="step", origin=None):
    """Subtract from selected solids then optionally add; all remain disjoint."""
    result = []
    for solid in solids:
        lo, hi = solid["z_min_um"], solid["z_max_um"]
        if hi <= z0 or lo >= z1 or (targets is not None and solid["material"] not in targets):
            result.append(solid)
            continue
        if lo < z0:
            result.append({**solid, "z_max_um": z0})
        if hi > z1:
            result.append({**solid, "z_min_um": z1})
        remainder = _simple(_boolean([_gds().Polygon(solid["polygon_xy_um"])], mask, "not"))
        for poly in remainder:
            result.append({**solid, "polygon_xy_um": poly.points.tolist(), "z_min_um": max(lo, z0), "z_max_um": min(hi, z1)})
    if material is not None:
        for i, poly in enumerate(_simple(_boolean(mask, [], "or"))):
            result.append({"name": f"{name}_{i}", "material": material, "polygon_xy_um": poly.points.tolist(),
                           "z_min_um": z0, "z_max_um": z1, **(origin or {})})
    return result


def build_geometry(recipe, *, base_dir=".", snapshots_dir=None):
    """Apply create_substrate/create_film/add/etch/delete in explicit sequence.

    Add/absolute film replaces existing material in its volume. Surface films
    add only to empty space, following an explicitly selected geometric rule.
    Etch/delete removes only
    named target materials and fills their volume with the background, crossing
    layers only inside the explicit depth interval. The mask itself is not a
    material. All operations use 90-degree walls; other angles are rejected.
    """
    if recipe.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported ProcessRecipe schema_version")
    from .marks import validate_recipe_mark
    mark = validate_recipe_mark(recipe, base_dir)
    invariant_y = _bool(recipe.get("invariant_y", False), "invariant_y")
    extend_bottom = _bool(recipe.get("substrate_extend_bottom", True), "substrate_extend_bottom")
    period_x = _finite(recipe["period_x_um"], "period_x_um")
    if period_x <= 0:
        raise ValueError("period_x_um must be positive")
    bounds = np.asarray(recipe["cell_bounds_um"], float)
    if bounds.shape != (3, 2) or not np.all(np.isfinite(bounds)) or np.any(bounds[:, 1] <= bounds[:, 0]):
        raise ValueError("cell_bounds_um must contain three finite increasing intervals")
    if not np.isclose(np.diff(bounds[0])[0], period_x, atol=1e-9, rtol=0):
        raise ValueError("period_x_um must be explicitly specified and equal cell x extent")
    if recipe.get("period_y_um") is not None:
        period_y = _finite(recipe["period_y_um"], "period_y_um")
        if period_y <= 0 or not np.isclose(np.diff(bounds[1])[0], period_y, atol=1e-9, rtol=0):
            raise ValueError("period_y_um must be positive and equal cell y extent")
    solids, history = [], []
    substrate_material = recipe.get("substrate_material")
    snapshots = []
    ids = [s.get("id", "") for s in recipe["steps"]]
    if any(not isinstance(s, str) or not s for s in ids) or len(set(ids)) != len(ids):
        raise ValueError("Process steps require unique nonempty IDs")
    for step_index, step in enumerate(recipe["steps"]):
        origin = {"source_step_id": step["id"], "source_step_name": step.get("name") or step["id"],
                  "source_step_index": step_index}
        history_origin = {"id": step["id"], "name": origin["source_step_name"], "index": step_index}
        if not _bool(step.get("enabled", True), f"{step['id']}.enabled"):
            history.append({**history_origin, "operation": step.get("operation"), "enabled": False})
            snapshots.append((step["id"], copy.deepcopy(solids), copy.deepcopy(history)))
            continue
        if _finite(step.get("sidewall_angle_deg", 90), "sidewall_angle_deg") != 90:
            raise ValueError(f"{step['id']}: non-90 degree sidewalls are saved future parameters but not executable")
        op = step["operation"]
        if op not in ("create_substrate", "create_film", "add", "etch", "delete"):
            raise ValueError(f"{step['id']}: operation {op!r} not implemented")
        mask, source = _mask(step, recipe, base_dir)
        if op == "create_film":
            from .film_geometry import FILM_MODES, surface_film
            mode = step.get("film_mode", "absolute")
            if mode not in FILM_MODES:
                raise ValueError(f"{step['id']}: film_mode must be one of {FILM_MODES}")
            if mode != "absolute":
                additions, rules = surface_film(solids, recipe, step)
                for part_index, part in enumerate(additions):
                    # Existing background-labelled blocks represent air/void.
                    # Only those blocks are trimmed; actual material is kept.
                    solids = _rewrite(solids, [_gds().Polygon(part["polygon_xy_um"])],
                                      part["z_min_um"], part["z_max_um"], material=step["material"],
                                      targets=[recipe["background_material"]],
                                      name=f"{origin['source_step_name']}_{part_index}", origin=origin)
                history.append({**history_origin, "operation": op, "enabled": True, "source": source,
                                **rules, "z_interval_um": [min(p["z_min_um"] for p in additions),
                                                           max(p["z_max_um"] for p in additions)],
                                "solid_count": len(solids)})
                snapshots.append((step["id"], copy.deepcopy(solids), copy.deepcopy(history)))
                continue
        if op in ("etch", "delete"):
            targets = step.get("target_materials")
            if not isinstance(targets, list) or not targets:
                raise ValueError("Etch/delete requires an explicit nonempty target_materials list")
            if op == "etch":
                z1 = _finite(step["start_z_um"], "start_z_um")
                depth = _finite(step["etch_depth_um"], "etch_depth_um")
                if not np.isfinite(depth) or depth <= 0:
                    raise ValueError("Etch depth must be positive and finite")
                z0 = z1 - depth
            else:
                z0, z1 = _finite(step["z_min_um"], "z_min_um"), _finite(step["z_max_um"], "z_max_um")
            material = None
        else:
            targets = None
            material = step["material"]
            z0 = _finite(step["z_min_um"], "z_min_um")
            z1 = _finite(step["z_max_um"], "z_max_um") if "z_max_um" in step else z0 + _finite(step.get("thickness_um", 0), "thickness_um")
            if op == "create_substrate":
                substrate_material = material
        if not np.isfinite([z0, z1]).all() or z1 <= z0:
            raise ValueError(f"{step['id']}: invalid z interval")
        if z0 < bounds[2, 0] - 1e-9 or z1 > bounds[2, 1] + 1e-9:
            raise ValueError(f"{step['id']}: operation exceeds explicitly declared geometry z bounds")
        solids = _rewrite(solids, mask, z0, z1, material=material, targets=targets,
                          name=origin["source_step_name"], origin=origin)
        history.append({**history_origin, "operation": op, "enabled": True, "source": source,
                        "rule": "replace_volume" if material is not None else "remove_named_materials_to_background",
                        "z_interval_um": [z0, z1], "solid_count": len(solids),
                        "cd_bias_definition": "total linewidth change; mask offset=cd_bias_um/2"})
        if op == "create_film":
            history[-1].update({"film_mode": "absolute", "warnings": [
                f"{origin['source_step_name']}：使用旧版绝对z范围薄膜；不会自动贴合下方结构，重叠处替换原材料。"
                "如需贴合光栅，请选择等厚包覆或填平模式。"]})
        snapshots.append((step["id"], copy.deepcopy(solids), copy.deepcopy(history)))
    geometry = {"schema_version": SCHEMA_VERSION, "model_version": MODEL_VERSION,
                "representation": "parametric_solids", "units": "um",
                "period_x_um": period_x, "period_y_um": recipe.get("period_y_um"),
                "cell_bounds_um": bounds.tolist(), "invariant_y": invariant_y,
                "background_material": recipe["background_material"], "substrate_material": substrate_material,
                "substrate_extend_bottom": extend_bottom,
                "solids": solids, "process_history": history,
                "warnings": [warning for entry in history for warning in entry.get("warnings", [])],
                "coordinate_convention": "physical x=period/scan, y=stripe, z=height up; GDS xy",
                "input_hash": hashlib.sha256(json.dumps({"recipe": recipe, "sources": history}, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                "software_versions": {"python": sys.version.split()[0], "numpy": np.__version__, "gdstk": _gds().__version__},
                "provenance": {"source": "gds_and_simplified_geometric_process", "recipe": copy.deepcopy(recipe),
                               "not_process_physics": True, "boolean_precision_um": PRECISION_UM}}
    if mark is not None:
        geometry["mark"] = mark
    validate_geometry(geometry)
    geometry["invariance_evidence"] = invariance_evidence(geometry)
    if snapshots_dir is not None:
        folder = Path(snapshots_dir)
        folder.mkdir(parents=True, exist_ok=True)
        for index, (step_id, parts, log) in enumerate(snapshots):
            safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", step_id)[:64]
            snapshot = {**geometry, "solids": parts, "process_history": log, "snapshot_step_id": step_id}
            snapshot["warnings"] = [warning for entry in log for warning in entry.get("warnings", [])]
            snapshot["invariance_evidence"] = invariance_evidence(snapshot)
            save_geometry(snapshot, folder / f"step_{index + 1:03d}_{safe_id}.json")
    return geometry


def validate_geometry(geometry, *, check_disjoint=True):
    if geometry.get("schema_version") != SCHEMA_VERSION or geometry.get("units") != "um":
        raise ValueError("Geometry requires schema_version=1.0 and units=um")
    if geometry.get("representation") != "parametric_solids":
        raise ValueError("This operation expects parametric_solids")
    bounds = np.asarray(geometry["cell_bounds_um"], float)
    if bounds.shape != (3, 2) or not np.isfinite(bounds).all() or np.any(bounds[:, 1] <= bounds[:, 0]):
        raise ValueError("Geometry bounds must be three increasing finite intervals")
    for solid in geometry["solids"]:
        p = np.asarray(solid["polygon_xy_um"], float)
        if p.ndim != 2 or p.shape[1] != 2 or len(p) < 3 or not np.isfinite(p).all():
            raise ValueError("Invalid solid polygon")
        if not np.isfinite([solid["z_max_um"], solid["z_min_um"]]).all() or solid["z_max_um"] <= solid["z_min_um"]:
            raise ValueError("Invalid solid height")
        if solid["z_min_um"] < bounds[2, 0] - 1e-7 or solid["z_max_um"] > bounds[2, 1] + 1e-7:
            raise ValueError("Solid height extends outside cell")
        for key in ("source_step_id", "source_step_name"):
            if key in solid and (not isinstance(solid[key], str) or not solid[key]):
                raise ValueError(f"{key} must be a nonempty string when provided")
        if "source_step_index" in solid and (type(solid["source_step_index"]) is not int or solid["source_step_index"] < 0):
            raise ValueError("source_step_index must be a nonnegative integer when provided")
        if np.any(p.min(axis=0) < bounds[:2, 0] - 1e-7) or np.any(p.max(axis=0) > bounds[:2, 1] + 1e-7):
            raise ValueError("Polygon extends outside cell")
    if check_disjoint:
        for i, a in enumerate(geometry["solids"]):
            for b in geometry["solids"][i + 1:]:
                if min(a["z_max_um"], b["z_max_um"]) <= max(a["z_min_um"], b["z_min_um"]) + 1e-10:
                    continue
                overlap = _boolean([_gds().Polygon(a["polygon_xy_um"])], [_gds().Polygon(b["polygon_xy_um"])], "and")
                if sum(p.area() for p in overlap) > 1e-8:
                    raise ValueError("Geometry solids overlap; explicit Boolean partition required")
    return geometry


def save_geometry(geometry, path):
    path = Path(path)
    if path.suffix.lower() != ".json":
        raise ValueError("Geometry exchange output must be .json; raw GDS/image files are read-only inputs")
    destination = path.resolve()
    sources = [h.get("source", {}).get("path") for h in geometry.get("process_history", [])]
    if any(source and Path(source).resolve() == destination for source in sources):
        raise ValueError("Refusing to overwrite an original geometry source")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(geometry, indent=2, ensure_ascii=False, allow_nan=False)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=path.name + ".", suffix=".tmp", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def load_geometry(path):
    # No GDS dependency for already partitioned, validated exchange data.
    geometry = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return validate_geometry(geometry, check_disjoint=False)


def geometry_volume_by_material(geometry):
    volumes = {}
    for solid in geometry["solids"]:
        p = np.asarray(solid["polygon_xy_um"])
        area = abs(np.dot(p[:, 0], np.roll(p[:, 1], 1)) - np.dot(p[:, 1], np.roll(p[:, 0], 1))) / 2
        volumes[solid["material"]] = volumes.get(solid["material"], 0) + area * (solid["z_max_um"] - solid["z_min_um"])
    return volumes


def preview_geometry(geometry, folder):
    """Render saved geometry through the independent step-based preview module."""
    from .geometry_preview import preview_geometry as render
    return render(geometry, folder)
