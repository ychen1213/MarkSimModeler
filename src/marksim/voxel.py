"""Discrete material-label volumes, physical xyz micrometres, no ID interpolation."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile

import h5py
import numpy as np

from .schemas import canonical_hash, file_hash

MODEL_VERSION = "voxel-labels-1.0"


def grid_shape(bounds, spacing, max_voxels):
    bounds, spacing = np.asarray(bounds, float), np.asarray(spacing, float)
    if bounds.shape != (3, 2) or not np.isfinite(bounds).all() or np.any(bounds[:, 1] <= bounds[:, 0]):
        raise ValueError("Voxel bounds must be three finite increasing intervals")
    if spacing.shape != (3,) or not np.isfinite(spacing).all() or np.any(spacing <= 0):
        raise ValueError("spacing_um needs three finite positive entries")
    with np.errstate(over="ignore", divide="ignore"):
        ratio = (bounds[:, 1] - bounds[:, 0]) / spacing
    if not np.isfinite(ratio).all():
        raise ValueError("Voxel dimensions overflow; increase spacing")
    shape = [max(1, math.ceil(float(r) - 1e-12)) for r in ratio]
    if math.prod(shape) > max_voxels:
        raise ValueError("Voxel count exceeds max_voxels")
    return np.asarray(shape, dtype=np.int64)


def valid_polygon(points):
    """Require a finite, simple, positive-area polygon before rasterization."""
    p = np.asarray(points, float)
    if p.ndim != 2 or p.shape[1] != 2 or len(p) < 3 or not np.isfinite(p).all():
        raise ValueError("Polygon needs at least three finite xy points")
    if np.array_equal(p[0], p[-1]):
        p = p[:-1]
    if len(p) < 3 or len(np.unique(p, axis=0)) != len(p):
        raise ValueError("Polygon vertices must be distinct")
    area = abs(np.sum(p[:, 0] * np.roll(p[:, 1], -1) - p[:, 1] * np.roll(p[:, 0], -1))) / 2
    if area <= 1e-14:
        raise ValueError("Polygon requires positive area; collinear/degenerate contours are invalid")
    def cross(a, b, c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    for i in range(len(p)):
        a, b = p[i], p[(i+1) % len(p)]
        for j in range(i+1, len(p)):
            if j == i+1 or (i == 0 and j == len(p)-1):
                continue
            c, d = p[j], p[(j+1) % len(p)]
            if np.any(np.maximum(np.minimum(a, b), np.minimum(c, d)) > np.minimum(np.maximum(a, b), np.maximum(c, d)) + 1e-12):
                continue
            if cross(a, b, c) * cross(a, b, d) <= 1e-24 and cross(c, d, a) * cross(c, d, b) <= 1e-24:
                raise ValueError("Polygon must be simple: intersecting or touching nonadjacent edges are invalid")
    return p


def _metadata(model):
    return {k: copy.deepcopy(v) for k, v in model.items() if k != "material_id"}


def create_voxel_model(material_id, origin_um, spacing_um, material_map, *, background_id=0, provenance=None):
    ids = np.asarray(material_id)
    origin, spacing = np.asarray(origin_um, float), np.asarray(spacing_um, float)
    if ids.ndim != 3 or not ids.size or not np.issubdtype(ids.dtype, np.integer) or ids.min() < 0 or ids.max() > 65535:
        raise ValueError("material_id must be a nonempty 3D integer xyz array with IDs 0..65535")
    if origin.shape != (3,) or spacing.shape != (3,) or not np.isfinite([origin, spacing]).all() or np.any(spacing <= 0):
        raise ValueError("origin_um and spacing_um require finite xyz triples; spacing must be positive")
    mapping = {}
    for key, name in material_map.items():
        if isinstance(key, bool) or not str(key).isdigit() or not isinstance(name, str) or not name:
            raise ValueError("material_map needs nonnegative integer IDs and material names")
        mapping[int(key)] = name
    if type(background_id) is not int or background_id not in mapping:
        raise ValueError("background_id must be an integer present in material_map")
    if not set(map(int, np.unique(ids))).issubset(mapping):
        raise ValueError("Every voxel label must have a material_map entry")
    ids = np.ascontiguousarray(ids, dtype=np.uint16)
    upper = origin + np.asarray(ids.shape) * spacing
    provenance = copy.deepcopy(provenance or {"source": "explicit_integer_labels"})
    result = {"schema_version": "1.0", "model_version": MODEL_VERSION, "representation": "voxel_labels",
              "units": "um", "axis_order": "xyz", "origin_kind": "lower_cell_corner",
              "origin_um": origin.tolist(), "spacing_um": spacing.tolist(), "shape_xyz": list(ids.shape),
              "cell_bounds_um": np.column_stack([origin, upper]).tolist(),
              "material_map": {str(k): v for k, v in sorted(mapping.items())},
              "background_id": background_id, "background_material": mapping[background_id],
              "material_id": ids, "invariant_y": False, "provenance": provenance,
              "generation_method": provenance.get("generation_method", "discrete_geometry_conversion"),
              "solver_scope": "geometry interchange; full Jones FDTD does not accept reconstructed voxels in MVP",
              "coordinate_convention": "physical x=scan, y=stripe, z=up; cell center=origin+(index+0.5)*spacing"}
    result["content_hash"] = hashlib.sha256(ids.tobytes() + canonical_hash(_metadata(result)).encode()).hexdigest()
    return result


def validate_voxels(model):
    if model.get("schema_version") != "1.0" or model.get("representation") != "voxel_labels" or model.get("units") != "um" or model.get("axis_order") != "xyz":
        raise ValueError("Voxel exchange requires schema 1.0, voxel_labels, units=um, axis_order=xyz")
    checked = create_voxel_model(model["material_id"], model["origin_um"], model["spacing_um"], model["material_map"],
                                 background_id=model["background_id"], provenance=model.get("provenance"))
    if list(np.shape(model["material_id"])) != model.get("shape_xyz"):
        raise ValueError("Voxel shape metadata disagrees with integer labels")
    if not np.allclose(checked["cell_bounds_um"], model["cell_bounds_um"], atol=1e-12, rtol=0):
        raise ValueError("Voxel bounds disagree with origin/spacing/shape")
    if checked["content_hash"] != model.get("content_hash"):
        raise ValueError("Voxel labels/coordinates/material map differ from the saved content hash")
    return model


def save_voxels(model, path):
    validate_voxels(model)
    path = Path(path)
    if path.suffix.lower() not in (".h5", ".hdf5"):
        raise ValueError("Voxel exchange output must be HDF5; source image/NPZ files are read-only")
    source = model.get("provenance", {}).get("source_path")
    if source and Path(source).resolve() == path.resolve():
        raise ValueError("Refusing to overwrite an original voxel source")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=path.name + ".", suffix=".tmp", dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
        with h5py.File(temporary, "w") as h:
            h.attrs["metadata_json"] = json.dumps(_metadata(model), ensure_ascii=False, allow_nan=False)
            h.attrs["schema_version"] = "1.0"
            h.attrs["axis_order"] = "xyz"
            h.attrs["units"] = "um"
            h.create_dataset("material_id", data=model["material_id"], compression="gzip", shuffle=True)
            h.create_dataset("origin_um", data=model["origin_um"])
            h.create_dataset("spacing_um", data=model["spacing_um"])
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return str(path)


def load_voxels(path):
    with h5py.File(path, "r") as h:
        model = json.loads(h.attrs["metadata_json"])
        model["material_id"] = h["material_id"][...]
        for key in ("origin_um", "spacing_um"):
            if not np.array_equal(h[key][...], model[key]):
                raise ValueError(f"HDF5 {key} dataset differs from metadata")
    return validate_voxels(model)


def geometry_manifest(model, voxel_file):
    validate_voxels(model)
    return {**_metadata(model), "voxel_file": str(voxel_file), "datasets": {"material_id": "material_id", "origin_um": "origin_um", "spacing_um": "spacing_um"}}


def import_legacy_npz(path):
    """Import actual grating3d NPZ: nm cell centers -> um lower-corner origin."""
    path = Path(path).resolve()
    with np.load(path, allow_pickle=False) as h:
        if "axis_order" not in h or str(h["axis_order"].item()) != "xyz":
            raise ValueError("Legacy NPZ must explicitly declare xyz axes")
        ids = h["material_id"].copy()
        spacing = np.asarray(h["spacing_nm"], float)
        if spacing.shape != (3,) or not np.isfinite(spacing).all() or np.any(spacing <= 0):
            raise ValueError("Invalid legacy spacing_nm")
        centers = [np.asarray(h[a + "_nm"], float) for a in "xyz"]
        for a, c, step, count in zip("xyz", centers, spacing, ids.shape):
            if c.shape != (count,) or not np.isfinite(c).all() or (len(c) > 1 and not np.allclose(np.diff(c), step, atol=1e-8, rtol=1e-7)):
                raise ValueError(f"Legacy {a}_nm centers do not match shape/spacing")
        if "material_names" not in h:
            raise ValueError("Legacy NPZ needs explicit material_names; labels cannot identify optical material")
        names = h["material_names"].tolist()
        mapping = {i: str(n) for i, n in enumerate(names) if str(n)}
        background = int(h["background_material_id"].item()) if "background_material_id" in h else 0
    origin = (np.array([c[0] for c in centers]) - spacing / 2) / 1000
    return create_voxel_model(ids, origin, spacing / 1000, mapping, background_id=background,
            provenance={"source": "legacy_grating3d_npz", "source_path": str(path), "source_sha256": file_hash(path),
                        "conversion": "nm cell centers to um lower corner; xyz integer labels preserved exactly",
                        "not_imported": "Legacy epsilon/n/k arrays are not used as optical materials"})


def voxelize_parametric(geometry, spacing_um, *, max_voxels=2000000):
    """Center-sample disjoint solid geometry; report requested and actual spacing."""
    from matplotlib.path import Path as PolygonPath
    from .geometry import validate_geometry as validate_solids
    if geometry.get("representation") != "parametric_solids" or geometry.get("units") != "um":
        raise ValueError("Expected parametric_solids in um")
    validate_solids(geometry)
    bounds = np.asarray(geometry["cell_bounds_um"], float)
    step = np.asarray(spacing_um, float)
    shape = grid_shape(bounds, step, max_voxels)
    polygons = []
    for solid in geometry["solids"]:
        polygons.append(valid_polygon(solid["polygon_xy_um"]))
        if solid["z_min_um"] < bounds[2, 0] - 1e-12 or solid["z_max_um"] > bounds[2, 1] + 1e-12:
            raise ValueError("Solid height extends outside the declared voxel cell")
    actual = (bounds[:, 1] - bounds[:, 0]) / shape
    axes = [lo + (np.arange(n) + .5) * d for lo, n, d in zip(bounds[:, 0], shape, actual)]
    names = [geometry["background_material"]] + sorted({s["material"] for s in geometry["solids"]} - {geometry["background_material"]})
    mapping = dict(enumerate(names))
    lookup = {n: i for i, n in mapping.items()}
    labels = np.zeros(tuple(shape), dtype=np.uint16)
    x, y = np.meshgrid(axes[0], axes[1], indexing="ij")
    points = np.column_stack([x.ravel(), y.ravel()])
    for solid, polygon in zip(geometry["solids"], polygons):
        inside_xy = PolygonPath(polygon).contains_points(points).reshape(shape[:2])
        inside_z = (axes[2] >= solid["z_min_um"]) & (axes[2] < solid["z_max_um"])
        labels[inside_xy[:, :, None] & inside_z[None, None, :]] = lookup[solid["material"]]
    return create_voxel_model(labels, bounds[:, 0], actual, mapping,
            provenance={"source": "parametric_center_sampling", "geometry_input_hash": geometry.get("input_hash", canonical_hash(geometry)),
                        "requested_spacing_um": step.tolist(), "assumptions": ["Cell-center occupancy discretization; not a new physical measurement"]})


def volume_fractions(model):
    ids, counts = np.unique(model["material_id"], return_counts=True)
    return {str(int(i)): {"material": model["material_map"][str(int(i))], "count": int(c),
                         "fraction": float(c / model["material_id"].size),
                         "volume_um3": float(c * np.prod(model["spacing_um"]))} for i, c in zip(ids, counts)}


def sample_labels(model, points_um):
    """Piecewise constant half-open cells; upper outer boundary is background."""
    points = np.asarray(points_um, float)
    if points.shape[-1] != 3 or not np.isfinite(points).all():
        raise ValueError("Sample points must be finite xyz coordinates")
    indices = np.floor((points - model["origin_um"]) / model["spacing_um"]).astype(np.int64)
    shape = np.asarray(model["material_id"].shape)
    inside = np.all((indices >= 0) & (indices < shape), axis=-1)
    clipped = np.clip(indices, 0, shape - 1)
    labels = model["material_id"][clipped[..., 0], clipped[..., 1], clipped[..., 2]]
    return np.where(inside, labels, model["background_id"]).astype(np.uint16)


def make_lossless_material_function(model, evaluated_materials, mp, *, center_um=None):
    """Create one cached Medium per material; never one Python object per voxel.

    Physical and 3D Meep xyz axes agree. p_phys = p_meep + center_um.
    The function is only a small, nondispersive conversion adapter, not Jones.
    """
    validate_voxels(model)
    center = np.zeros(3) if center_um is None else np.asarray(center_um, float)
    if center.shape != (3,) or not np.isfinite(center).all():
        raise ValueError("center_um must be finite xyz")
    media = {}
    for ident, name in model["material_map"].items():
        if name not in evaluated_materials:
            raise ValueError(f"Missing optical material {name}")
        spec = evaluated_materials[name]
        er = float(spec["epsilon_real"])
        ei, k = float(spec.get("epsilon_imag", 0)), float(spec.get("k", 0))
        if not np.isfinite([er, ei, k]).all() or er <= .75 * (1 + 1e-8) or ei != 0 or k != 0 or spec.get("D_conductivity", 0) != 0:
            raise ValueError("Voxel Meep conversion requires lossless nondispersive media and epsilon_real>0.75 for fixed 3D Courant=0.5")
        if "n" in spec and (not np.isfinite(spec["n"]) or spec["n"] <= 0 or not np.isclose(float(spec["n"])**2, er)):
            raise ValueError("Voxel material has inconsistent lossless n and epsilon_real")
        media[int(ident)] = mp.Medium(epsilon=er)
    def material_function(point):
        label = int(sample_labels(model, np.array([point.x, point.y, point.z]) + center))
        return media[label]
    material_function.materials_by_id = media
    material_function.physical_center_um = center
    material_function.courant = .5
    return material_function


def preview_voxels(model, folder):
    """Exact nearest-label cross sections and sampled 3D material cell centers."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap, BoundaryNorm
    import plotly.graph_objects as go
    validate_voxels(model)
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    ids = model["material_id"]
    keys = sorted(int(k) for k in model["material_map"])
    palette = ["#e6edf5", "#4c78a8", "#f58518", "#54a24b", "#e45756", "#b279a2"]
    display = np.searchsorted(keys, ids)
    cmap = ListedColormap([palette[i % len(palette)] for i in range(len(keys))])
    norm = BoundaryNorm(np.arange(len(keys) + 1) - .5, len(keys))
    bounds = model["cell_bounds_um"]
    fig, axes = plt.subplots(1, 3, figsize=(13, 4))
    for ax, array, a, b, title in ((axes[0], display[:, :, ids.shape[2]//2].T, 0, 1, "x-y central z"),
                                  (axes[1], display[:, ids.shape[1]//2, :].T, 0, 2, "x-z central y"),
                                  (axes[2], display[ids.shape[0]//2, :, :].T, 1, 2, "y-z central x")):
        im = ax.imshow(array, origin="lower", interpolation="nearest", extent=[*bounds[a], *bounds[b]], cmap=cmap, norm=norm, aspect="auto")
        ax.set(title=title, xlabel="xyz"[a]+" [um]", ylabel="xyz"[b]+" [um]")
    colorbar = fig.colorbar(im, ax=axes.tolist(), ticks=np.arange(len(keys)), shrink=.8)
    colorbar.ax.set_yticklabels([f"{k}: {model['material_map'][str(k)]}" for k in keys])
    fig.savefig(folder / "voxel_sections.png", dpi=140, bbox_inches="tight"); plt.close(fig)
    figure = go.Figure()
    for j, ident in enumerate(keys):
        if ident == model["background_id"]:
            continue
        positions = np.argwhere(ids == ident)
        stride = max(1, int(np.ceil(len(positions)/12000)))
        xyz = np.asarray(model["origin_um"]) + (positions[::stride] + .5) * model["spacing_um"]
        figure.add_trace(go.Scatter3d(x=xyz[:, 0], y=xyz[:, 1], z=xyz[:, 2], mode="markers",
                          marker=dict(size=2, color=palette[j % len(palette)], opacity=.45),
                          name=f"{ident}: {model['material_map'][str(ident)]} (every {stride} cells)"))
    figure.update_layout(title="Discrete material voxel centers (display sampling only; full labels preserved in HDF5)",
                         scene=dict(xaxis_title="x [um]", yaxis_title="y [um]", zaxis_title="z [um]", aspectmode="data"))
    figure.write_html(folder / "voxel_3d.html", include_plotlyjs=True)
    return {"sections": str(folder / "voxel_sections.png"), "html": str(folder / "voxel_3d.html")}
