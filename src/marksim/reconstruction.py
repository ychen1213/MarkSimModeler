"""Parallel SEM/TEM entrance reusing the existing local reconstruction package.

This module produces measured/template geometry and discrete labels. It does
not send nonideal reconstructed geometry to the validated periodic Jones chain.
"""
from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
import sys

import numpy as np

from .schemas import canonical_hash, file_hash, write_json
from .voxel import (create_voxel_model, save_voxels, geometry_manifest, sample_labels,
                    preview_voxels, volume_fractions, grid_shape, valid_polygon)

MODEL_VERSION = "legacy-sem-tem-bridge-1.0"


def validate_registration(registration=None):
    reg = copy.deepcopy(registration or {"kind": "rigid", "matrix_3x3": np.eye(3).tolist(),
                       "description": "Identity coordinate assignment, not an experimentally fitted registration"})
    if reg.get("kind") not in ("rigid", "affine"):
        raise ValueError("Only traceable rigid or affine registration is supported; nonrigid warps are forbidden")
    matrix = np.asarray(reg.get("matrix_3x3"), float)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all() or not np.allclose(matrix[2], [0, 0, 1], atol=1e-12, rtol=0):
        raise ValueError("Registration requires a finite homogeneous 3x3 xy affine matrix")
    determinant = np.linalg.det(matrix[:2, :2])
    if abs(determinant) < 1e-10:
        raise ValueError("Registration matrix is singular")
    if reg["kind"] == "rigid" and (not np.allclose(matrix[:2, :2].T @ matrix[:2, :2], np.eye(2), atol=1e-8, rtol=0) or not np.isclose(determinant, 1, atol=1e-8, rtol=0)):
        raise ValueError("Rigid registration must contain a proper rotation plus translation, with determinant +1")
    for key in ("source_landmarks_um", "target_landmarks_um"):
        points = np.asarray(reg.get(key, []), float)
        if points.size and (points.ndim != 2 or points.shape[1] != 2 or not np.isfinite(points).all()):
            raise ValueError(f"{key} must be finite Nx2 um coordinates")
    source = np.asarray(reg.get("source_landmarks_um", []), float)
    target = np.asarray(reg.get("target_landmarks_um", []), float)
    if source.shape != target.shape:
        raise ValueError("Registration source/target landmarks must correspond one-to-one")
    if source.size:
        transformed = np.column_stack([source, np.ones(len(source))]) @ matrix.T
        reg["landmark_rms_residual_um"] = float(np.sqrt(np.mean(np.sum((transformed[:, :2] - target) ** 2, axis=1))))
    reg.update(matrix_3x3=matrix.tolist(), determinant_xy=float(determinant),
               coordinate_mapping="processed SEM calibrated local xy [um] -> physical project xy [um]",
               z_mapping="physical z=local reconstruction z+TEM height_reference_um",
               warning="Affine scale/shear changes geometry and must come from documented calibration, not fitting away actual mark displacement")
    return reg


def validate_tem_section(section, registration):
    if section is None:
        raise ValueError("Record TEM origin_phys_um, direction_xy and height_reference_um explicitly")
    section = copy.deepcopy(section)
    origin, direction = np.asarray(section.get("origin_phys_um"), float), np.asarray(section.get("direction_xy"), float)
    height = float(section.get("height_reference_um", np.nan))
    if origin.shape != (3,) or direction.shape != (2,) or not np.isfinite(origin).all() or not np.isfinite(direction).all() or not np.isfinite(height) or np.linalg.norm(direction) <= 0:
        raise ValueError("TEM section requires finite physical origin, nonzero xy direction and height reference")
    direction /= np.linalg.norm(direction)
    expected = np.asarray(registration["matrix_3x3"])[:2, 0]
    expected /= np.linalg.norm(expected)
    if not np.allclose(direction, expected, atol=1e-6, rtol=0):
        raise ValueError("Current ordered TEM template must follow the registered +local-x direction; other section directions require a different model")
    if section.get("correspondence", "ordered") not in ("ordered", "representative"):
        raise ValueError("TEM correspondence must be ordered or representative")
    section.update(origin_phys_um=origin.tolist(), direction_xy=direction.tolist(), height_reference_um=height,
                   section_plane_normal_phys=[-float(direction[1]), float(direction[0]), 0.],
                   model_usage="TEM heights/sidewall profile extended along local stripe y; section location is recorded, not additional measured 3D information")
    return section


def apply_registration(model, registration, *, z_offset_um=0.0, max_voxels=2000000):
    reg = validate_registration(registration)
    matrix = np.asarray(reg["matrix_3x3"])
    bounds = np.asarray(model["cell_bounds_um"])
    corners = np.array([[x, y, 1] for x in bounds[0] for y in bounds[1]])
    mapped = corners @ matrix.T
    provenance = {**copy.deepcopy(model["provenance"]), "registration": reg,
                  "height_reference_um": float(z_offset_um)}
    if np.allclose(matrix[:2, :2], np.eye(2), atol=1e-12, rtol=0):
        origin = np.asarray(model["origin_um"]) + [matrix[0, 2], matrix[1, 2], z_offset_um]
        result = create_voxel_model(model["material_id"].copy(), origin, model["spacing_um"], model["material_map"],
                                    background_id=model["background_id"], provenance={**provenance, "resampling": "none; exact translation of voxel corner origin"})
    else:
        lower = np.array([mapped[:, 0].min(), mapped[:, 1].min(), bounds[2, 0] + z_offset_um])
        upper = np.array([mapped[:, 0].max(), mapped[:, 1].max(), bounds[2, 1] + z_offset_um])
        step = np.asarray(model["spacing_um"])
        shape = grid_shape(np.column_stack([lower, upper]), step, max_voxels)
        step = (upper - lower) / shape
        x, y = np.meshgrid(lower[0] + (np.arange(shape[0]) + .5) * step[0],
                           lower[1] + (np.arange(shape[1]) + .5) * step[1], indexing="ij")
        local = np.stack([x, y, np.ones_like(x)], axis=-1) @ np.linalg.inv(matrix).T
        labels = np.full(tuple(shape), model["background_id"], dtype=np.uint16)
        for iz in range(shape[2]):
            xyz = np.stack([local[..., 0], local[..., 1], np.full_like(x, lower[2] + (iz + .5) * step[2] - z_offset_um)], axis=-1)
            labels[:, :, iz] = sample_labels(model, xyz)
        result = create_voxel_model(labels, lower, step, model["material_map"], background_id=model["background_id"],
                                   provenance={**provenance, "resampling": "inverse affine, piecewise-constant source voxel lookup; no material-ID interpolation"})
    original, transformed = volume_fractions(model), volume_fractions(result)
    determinant = abs(reg["determinant_xy"])
    report = {}
    for key, value in original.items():
        if int(key) == model["background_id"]:
            continue
        expected = value["volume_um3"] * determinant
        actual = transformed.get(key, {"volume_um3": 0})["volume_um3"]
        report[key] = {"expected_affine_volume_um3": expected, "actual_raster_volume_um3": actual,
                       "absolute_error_um3": actual - expected}
    # This derived diagnostic is not used to alter labels or material values.
    result["registration_volume_diagnostic"] = report
    return result


def _legacy_modules(legacy_root):
    root = Path(legacy_root).resolve()
    if not (root / "grating3d" / "pipeline.py").is_file():
        raise FileNotFoundError(f"Existing reconstruction package not found at {root}")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    modules = {name: importlib.import_module("grating3d." + name) for name in ("pipeline", "geometry", "imaging")}
    for module in modules.values():
        if root not in Path(module.__file__).resolve().parents:
            raise RuntimeError("A different grating3d package is already loaded; restart before selecting another legacy root")
    return modules


def reconstruct_legacy(config, legacy_root, output_dir, *, material_map, tem_section,
                       registration=None, spacing_um=(.02, .02, .02), log=None, cancel_event=None):
    """Actual old image extraction -> new HDF5 labels, with explicit provenance.

    material_map roles are fixed: 0 background, 1 planar substrate, 2 ridges.
    Input can be an old saved config JSON path or a config dict. Real images
    require explicitly confirmed calibration. Demo generation writes NEW files.
    """
    log = log or (lambda message: None)
    modules = _legacy_modules(legacy_root)
    pipeline, old_geometry, imaging = (modules[k] for k in ("pipeline", "geometry", "imaging"))
    cfg = pipeline.load_config(config) if isinstance(config, (str, Path)) else copy.deepcopy(config)
    if type(cfg.get("generate_demo", False)) is not bool:
        raise ValueError("generate_demo must be a boolean; a string cannot select synthetic versus real image input")
    reg = validate_registration(registration)
    section = validate_tem_section(tem_section, reg)
    out = Path(output_dir).resolve()
    if out.exists() and any(out.iterdir()):
        raise FileExistsError("SEM/TEM reconstruction requires a new or empty output directory")
    if set(map(int, material_map)) != {0, 1, 2}:
        raise ValueError("material_map roles must be exactly 0=background, 1=substrate, 2=ridges")
    spacing = np.asarray(spacing_um, float)
    if spacing.shape != (3,) or not np.isfinite(spacing).all() or np.any(spacing <= 0):
        raise ValueError("spacing_um must contain three finite positive numbers")
    cfg["output_dir"] = str(out)
    cfg.setdefault("geometry", {})["tem_match"] = "ordered" if section.get("correspondence", "ordered") == "ordered" else "median"
    if not cfg.get("generate_demo", False):
        for name in ("sem", "tem"):
            calibration = cfg.get("calibration", {}).get(name, {})
            if calibration.get("confirmed") is not True:
                raise ValueError(f"{name.upper()}: real images require confirmed scale calibration; assumed demo nm/pixel cannot enter reconstruction")
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "status.json", {"schema_version": "1.0", "status": "running", "generation_method": "sem_tem_measurement_and_template"})
    try:
        measured = pipeline.prepare_measurements(cfg, log=log, cancel_event=cancel_event)
        geom = measured["geometry"]
        grid_cfg = {"grid": {**cfg.get("grid", {}), "spacing_nm": (spacing * 1000).tolist()}}
        estimate = pipeline.estimate_grid(geom, grid_cfg)
        limit = int(cfg.get("grid", {}).get("max_voxels", 2000000))
        if estimate["voxel_count"] > limit:
            raise ValueError(f"SEM/TEM labels require {estimate['voxel_count']} voxels, exceeding max_voxels={limit}")
        bounds = np.asarray(estimate["bounds_nm"])
        shape = np.asarray(estimate["shape_xyz"])
        actual = (bounds[:, 1] - bounds[:, 0]) / shape
        axes = [lo + (np.arange(n) + .5) * step for lo, n, step in zip(bounds[:, 0], shape, actual)]
        labels = np.zeros(tuple(shape), dtype=np.uint16)
        labels[:, :, axes[2] < 0] = 1
        labels[old_geometry.ridge_mask(geom, *axes)] = 2
        origins = {name: {"path": measured["config"][name + "_image"], "sha256": file_hash(measured["config"][name + "_image"])} for name in ("sem", "tem")}
        source = create_voxel_model(labels, bounds[:, 0] / 1000, actual / 1000, material_map,
                    provenance={"source": "legacy_sem_tem_measurements", "generation_method": "synthetic_image_measurement" if cfg.get("generate_demo") else "real_image_measurement_with_template",
                                "source_images": origins, "legacy_root": str(Path(legacy_root).resolve()), "bridge_version": MODEL_VERSION,
                                "calibration": measured["config"].get("calibration", {"demo_nm_per_pixel": [cfg.get("sem_nm_per_pixel"), cfg.get("tem_nm_per_pixel")]}),
                                "sem_preprocessing": measured["sem"]["preprocess_metadata"], "tem_preprocessing": measured["tem"]["preprocess_metadata"],
                                "tem_section": section, "material_assignment": "user labels: background/substrate/ridges, not image-inferred composition",
                                "assumptions": geom.get("assumptions", []) + ["TEM profile is extended along local y; no unique arbitrary 3D recovery from two images"],
                                "config_snapshot": pipeline.json_ready(measured["config"])})
        save_voxels(source, out / "voxels_local.h5")
        registered = apply_registration(source, reg, z_offset_um=section["height_reference_um"], max_voxels=limit)
        save_voxels(registered, out / "voxels.h5")
        write_json(out / "geometry.json", geometry_manifest(registered, "voxels.h5"))
        imaging.plot_edge_overlays(measured["sem"], measured["tem"], out)
        old_geometry.plot_preview(geom, out / "legacy_template_preview.png")
        previews = preview_voxels(registered, out)
        metrics = {"schema_version": "1.0", "sem": pipeline.json_ready(measured["sem"]["metrics"]),
                   "tem": pipeline.json_ready(measured["tem"]["metrics"]), "quality": pipeline.json_ready(measured["quality"]),
                   "tem_section": section, "registration": reg, "voxel_materials": volume_fractions(registered),
                   "registration_volume_diagnostic": registered["registration_volume_diagnostic"]}
        write_json(out / "measurements.json", metrics)
        np.savez_compressed(out / "measured_contours.npz", y_nm=geom["y_nm"], left_top_nm=geom["left_top_nm"],
                            right_top_nm=geom["right_top_nm"], heights_nm=geom["ridge_heights_nm"],
                            measured_valid=measured["sem"]["edge_valid_mask"],
                            interpolated=measured["sem"]["edge_interpolated_mask"], active=measured["sem"]["ridge_active"])
        if cancel_event is not None and cancel_event.is_set():
            raise pipeline.PipelineCancelled("Cancelled before final SEM/TEM result acceptance")
        write_json(out / "status.json", {"schema_version": "1.0", "status": "succeeded", "model_version": MODEL_VERSION,
                   "generation_method": registered["generation_method"], "full_jones_solver_supported": False})
        return {"geometry_file": str(out / "geometry.json"), "voxels_file": str(out / "voxels.h5"),
                "preview_files": previews, "measurements_file": str(out / "measurements.json"), "quality": metrics["quality"]}
    except Exception as exc:
        write_json(out / "status.json", {"schema_version": "1.0", "status": "cancelled" if isinstance(exc, pipeline.PipelineCancelled) else "failed", "error": str(exc)})
        raise


def extrude_manual_polygons(polygons_xy_um, *, z_min_um, z_max_um, bounds_um, spacing_um,
                            material_map, material_id=1, background_id=0, provenance=None):
    """Minimal manual 2.5D entrance: supplied physical contours -> vertical mask.

    This deliberately supports finite polygon extrusion only, with 90-degree
    walls. Manual contours are not silently classified as microscope edges.
    """
    from .voxel import voxelize_parametric
    mapping = {int(k): v for k, v in material_map.items()}
    if material_id not in mapping or background_id not in mapping:
        raise ValueError("Manual extrusion material IDs must be explicitly mapped")
    if material_id == background_id or mapping[material_id] == mapping[background_id]:
        raise ValueError("Manual foreground must use a distinct material ID and material name from the background")
    bounds = np.asarray(bounds_um, float)
    if bounds.shape != (3, 2) or not np.isfinite(bounds).all() or np.any(bounds[:, 1] <= bounds[:, 0]) or not np.isfinite([z_min_um, z_max_um]).all() or z_max_um <= z_min_um:
        raise ValueError("Manual extrusion requires finite bounds and increasing z interval")
    if z_min_um < bounds[2, 0] or z_max_um > bounds[2, 1]:
        raise ValueError("Manual height interval must lie inside the declared z bounds")
    if not polygons_xy_um:
        raise ValueError("At least one nonempty manual polygon is required")
    solids = []
    for i, points in enumerate(polygons_xy_um):
        points = valid_polygon(points)
        if np.any(points.min(axis=0) < bounds[:2, 0]) or np.any(points.max(axis=0) > bounds[:2, 1]):
            raise ValueError("Manual polygon must lie inside the declared xy bounds; crop explicitly before extrusion")
        solids.append({"name": f"manual_{i}", "material": mapping[material_id], "polygon_xy_um": points.tolist(), "z_min_um": z_min_um, "z_max_um": z_max_um})
    geometry = {"schema_version": "1.0", "representation": "parametric_solids", "units": "um", "background_material": mapping[background_id], "solids": solids, "cell_bounds_um": bounds.tolist()}
    model = voxelize_parametric(geometry, spacing_um)
    if np.all(model["material_id"] == model["background_id"]):
        raise ValueError("Manual foreground is empty on the chosen voxel grid; refine spacing or correct polygon/height")
    labels = np.where(model["material_id"] == model["background_id"], background_id, material_id)
    converted = {background_id: mapping[background_id], material_id: mapping[material_id]}
    return create_voxel_model(labels, model["origin_um"], model["spacing_um"], converted,
                              background_id=background_id, provenance={"source": "manual_polygon_2p5d_extrusion", "generation_method": "manual_geometry",
                               "manual_contours_xy_um": [np.asarray(p).tolist() for p in polygons_xy_um], "z_interval_um": [z_min_um, z_max_um],
                               "assumptions": ["Manual contour and uniform 90-degree extrusion; not automatically measured 3D"], **(provenance or {})})
