"""Parametric grating geometry and closed ridge mesh exports (units: nm)."""
from pathlib import Path
import struct

import numpy as np


def depth_offsets(geometry, ridge, depth):
    """Physical sidewall displacements from the SEM top; depth=0 top, 1 base."""
    depth = np.asarray(depth, dtype=float)
    profiles = geometry.get("sidewall_profiles")
    if profiles:
        profile = profiles[ridge]
        left = np.interp(depth, profile["depth_fraction"], profile["left_outward_nm"])
        right = np.interp(depth, profile["depth_fraction"], profile["right_outward_nm"])
    else:
        left = geometry["bottom_left_offset_nm"][ridge] * depth
        right = geometry["bottom_right_offset_nm"][ridge] * depth
    bow = 4 * geometry["bowing_nm"][ridge] * depth * (1 - depth)
    return left + bow, right + bow


def active_intervals(geometry, ridge):
    """Half-open physical intervals, including end caps around preserved gaps."""
    if "active_intervals_nm" in geometry:
        return geometry["active_intervals_nm"][ridge]
    return [geometry["y_bounds_nm"]]


def build_geometry(sem, tem, config=None):
    """Combine SEM top edges with TEM outward offsets and ridge heights.

    By default ridge correspondence is ordered left-to-right and the counts
    must agree. ``tem_match='median'`` explicitly applies the median observed
    TEM profile to every SEM ridge. ``bowing_nm`` adds an outward sidewall bow
    4*b*t*(1-t), where t is depth from ridge top to substrate.
    """
    config = config or {}
    if "geometry" in config:
        config = config["geometry"]
    left, right = np.asarray(sem["left_nm"], float), np.asarray(sem["right_nm"], float)
    y = np.asarray(sem["y_nm"], float)
    if left.shape != right.shape or left.ndim != 2 or left.shape[1] != len(y):
        raise ValueError("SEM edges must have shape (ridge_count, y_samples)")
    if not np.all(np.isfinite(left)) or not np.all(np.isfinite(right)) or not np.all(right > left):
        raise ValueError("SEM edges must be finite and have positive widths")
    if len(y) < 2 or not np.all(np.diff(y) > 0):
        raise ValueError("SEM y positions must increase and contain at least two samples")
    profiles = tem["per_ridge"]
    if not profiles:
        raise ValueError("No TEM profiles supplied")
    mode = config.get("tem_match", "ordered")
    if mode == "ordered":
        if len(profiles) != len(left):
            raise ValueError("Ordered SEM/TEM matching requires equal ridge counts; choose tem_match='median' explicitly to reuse a representative profile")
        heights = np.array([r["height_nm"] for r in profiles])
        lo = np.array([r["left_outward_nm"] for r in profiles])
        ro = np.array([r["right_outward_nm"] for r in profiles])
    elif mode == "median":
        heights = np.full(len(left), np.median([r["height_nm"] for r in profiles]))
        lo = np.full(len(left), np.median([r["left_outward_nm"] for r in profiles]))
        ro = np.full(len(left), np.median([r["right_outward_nm"] for r in profiles]))
    else:
        raise ValueError("tem_match must be ordered or median")
    if not np.all(np.isfinite(heights)) or not np.all(heights > 0):
        raise ValueError("TEM ridge heights must be positive and finite")
    bow = np.asarray(config.get("bowing_nm", 0.0), dtype=float)
    if bow.ndim == 0:
        bow = np.full(len(left), float(bow))
    if bow.shape != (len(left),) or not np.all(np.isfinite(bow)):
        raise ValueError("bowing_nm must be finite, scalar or one value per ridge")
    supplied_profiles = [r.get("sidewall_profile") for r in profiles]
    sidewall_profiles = None
    if all(p is not None for p in supplied_profiles):
        if mode == "ordered":
            sidewall_profiles = supplied_profiles
        else:
            depths = np.linspace(0, 1, 65)
            representative = {"depth_fraction": depths,
                "left_outward_nm": np.median([np.interp(depths,p["depth_fraction"],p["left_outward_nm"]) for p in supplied_profiles],axis=0),
                "right_outward_nm": np.median([np.interp(depths,p["depth_fraction"],p["right_outward_nm"]) for p in supplied_profiles],axis=0)}
            sidewall_profiles = [representative for _ in left]
        for p in sidewall_profiles:
            d = np.asarray(p["depth_fraction"])
            if d.ndim != 1 or len(d) < 2 or not np.all(np.diff(d)>0) or d[0]!=0 or d[-1]!=1:
                raise ValueError("Sidewall profiles must span increasing depth_fraction 0..1")
            for key in ("left_outward_nm","right_outward_nm"):
                values = np.asarray(p[key])
                if values.shape != d.shape or not np.all(np.isfinite(values)) or abs(values[0]) > 1e-6:
                    raise ValueError("Sidewall offsets must be finite and zero at ridge top")
    # A negative bow represents a neck. Verify widths and gaps through depth.
    for depth in np.linspace(0, 1, 33):
        shift = 4 * bow * depth * (1 - depth)
        ldis = np.array([np.interp(depth,p["depth_fraction"],p["left_outward_nm"]) for p in sidewall_profiles]) if sidewall_profiles else lo*depth
        rdis = np.array([np.interp(depth,p["depth_fraction"],p["right_outward_nm"]) for p in sidewall_profiles]) if sidewall_profiles else ro*depth
        lz = left - ldis[:, None] - shift[:, None]
        rz = right + rdis[:, None] + shift[:, None]
        if np.any(rz <= lz):
            raise ValueError("Sidewall model creates zero/negative ridge width")
        if len(left) > 1 and np.any(lz[1:] <= rz[:-1]):
            raise ValueError("Sidewall model makes neighboring ridges overlap")
    sx = float(sem.get("scale_x_nm",sem["nm_per_pixel"]))
    sy = float(sem.get("scale_y_nm",sem["nm_per_pixel"]))
    y_bounds = (float(y[0] - sy / 2), float(y[-1] + sy / 2))
    active = np.asarray(sem.get("ridge_active",np.ones(left.shape,dtype=bool)),dtype=bool)
    if active.shape != left.shape:
        raise ValueError("ridge_active must match the SEM contours")
    intervals = []
    for row in active:
        changes = np.diff(np.r_[False,row,False].astype(int))
        starts, stops = np.flatnonzero(changes==1), np.flatnonzero(changes==-1)
        intervals.append([(float(y[start]-sy/2),float(y[stop-1]+sy/2)) for start,stop in zip(starts,stops)])
    warnings = []
    if mode == "ordered":
        sem_widths = np.mean(right - left, axis=1)
        tem_widths = np.array([r["top_cd_nm"] for r in profiles])
        mismatch = np.abs(tem_widths - sem_widths) / sem_widths
        if np.any(mismatch > float(config.get("cd_mismatch_tolerance", 0.2))):
            warnings.append("SEM and TEM top CDs differ by more than the configured tolerance; verify scales and section correspondence")
    assumptions = ["SEM measured contours define the top surface at every y",
                   "TEM heights and bottom offsets are extended along y",
                   "z=0 is the substrate top; positive z points upward",
                   f"TEM ridge matching: {mode}",
                   "Preserved SEM gaps interrupt the full ridge height; hidden lower-layer continuity requires additional evidence",
                   "Bowing is a user-supplied model parameter, not inferred from these two views"]
    return {"y_nm": y, "left_top_nm": left, "right_top_nm": right,
            "height_nm": float(np.mean(heights)), "ridge_heights_nm": heights,
            "bottom_left_offset_nm": lo, "bottom_right_offset_nm": ro,
            "bowing_nm": bow, "ridge_count": len(left),
            "sidewall_profiles": sidewall_profiles, "active_intervals_nm": intervals,
            "x_bounds_nm": (0.0, float(sem.get("roi_shape",sem["image_shape"])[1] * sx)),
            "y_bounds_nm": y_bounds, "z_bounds_nm": (0.0, float(np.max(heights))),
            "tem_match": mode, "warnings": warnings, "assumptions": assumptions}


def ridge_mask(geometry, x_nm, y_nm, z_nm):
    """Return occupancy on supplied cell centres, with array axes (x, y, z)."""
    x, y, z = (np.asarray(axis, dtype=float) for axis in (x_nm, y_nm, z_nm))
    if any(axis.ndim != 1 or not np.all(np.isfinite(axis)) for axis in (x, y, z)):
        raise ValueError("Grid centre coordinates must be finite 1D arrays")
    mask = np.zeros((len(x), len(y), len(z)), dtype=bool)
    y_valid = (y >= geometry["y_bounds_nm"][0]) & (y <= geometry["y_bounds_nm"][1])
    for ridge in range(geometry["ridge_count"]):
        left = np.interp(y, geometry["y_nm"], geometry["left_top_nm"][ridge])
        right = np.interp(y, geometry["y_nm"], geometry["right_top_nm"][ridge])
        height = geometry["ridge_heights_nm"][ridge]
        depth = 1 - np.clip(z / height, 0, 1)
        lo, ro = depth_offsets(geometry,ridge,depth)
        lz = left[:, None] - lo[None, :]
        rz = right[:, None] + ro[None, :]
        intervals = active_intervals(geometry,ridge)
        active_y = np.zeros(len(y),dtype=bool)
        for start,stop in intervals:
            active_y |= (y >= start) & (y < stop)
        inside_z = (z >= 0) & (z <= height)
        mask |= ((x[:, None, None] >= lz[None, :, :]) & (x[:, None, None] <= rz[None, :, :])
                 & (y_valid & active_y)[None, :, None] & inside_z[None, None, :])
    return mask


def triangulate_ridges(geometry, y_stride=4, z_samples=9):
    """Return vertices (nm) and indexed triangles for separately closed ridges.

    Adjacent surface patches share vertex indices, including all end caps.
    Both straight and bowed sidewall models produce manifold closed meshes.
    """
    y_stride, z_samples = int(y_stride), int(z_samples)
    if y_stride < 1 or z_samples < 2:
        raise ValueError("y_stride >= 1 and z_samples >= 2 are required")
    raw_y = geometry["y_nm"]
    fractions = np.linspace(0, 1, z_samples)
    vertices, faces = [], []
    for ridge in range(geometry["ridge_count"]):
        for start, stop in active_intervals(geometry,ridge):
            y = np.unique(np.r_[start,raw_y[::y_stride][(raw_y[::y_stride]>start)&(raw_y[::y_stride]<stop)],stop])
            left = np.interp(y, raw_y, geometry["left_top_nm"][ridge])
            right = np.interp(y, raw_y, geometry["right_top_nm"][ridge])
            height = geometry["ridge_heights_nm"][ridge]
            z = fractions * height
            lo, ro = depth_offsets(geometry,ridge,1-fractions)
            lz = left[:, None] - lo[None, :]
            rz = right[:, None] + ro[None, :]
            base = len(vertices)
            for yi, yy in enumerate(y):
                for zi, zz in enumerate(z):
                    vertices.extend([(lz[yi, zi], yy, zz), (rz[yi, zi], yy, zz)])
            def index(yi, zi, side):
                return base + 2 * (yi * z_samples + zi) + side
            def quad(a, b, c, d):
                faces.extend([(a, b, c), (a, c, d)])
            for yi in range(len(y) - 1):
                for zi in range(z_samples - 1):
                    quad(index(yi, zi, 0), index(yi, zi + 1, 0), index(yi + 1, zi + 1, 0), index(yi + 1, zi, 0))
                    quad(index(yi, zi, 1), index(yi + 1, zi, 1), index(yi + 1, zi + 1, 1), index(yi, zi + 1, 1))
                quad(index(yi, 0, 0), index(yi + 1, 0, 0), index(yi + 1, 0, 1), index(yi, 0, 1))
                top = z_samples - 1
                quad(index(yi, top, 0), index(yi, top, 1), index(yi + 1, top, 1), index(yi + 1, top, 0))
            for zi in range(z_samples - 1):
                quad(index(0, zi, 0), index(0, zi, 1), index(0, zi + 1, 1), index(0, zi + 1, 0))
                end = len(y) - 1
                quad(index(end, zi, 0), index(end, zi + 1, 0), index(end, zi + 1, 1), index(end, zi, 1))
    return np.asarray(vertices, dtype=float), np.asarray(faces, dtype=np.int64)


def mesh_quality(vertices, faces):
    """Audit indexed-edge incidence, finite coordinates, and signed volume."""
    vertices, faces = np.asarray(vertices), np.asarray(faces)
    tri = vertices[faces]
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    area = np.linalg.norm(cross, axis=1) / 2
    edges = np.sort(np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]), axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    volume = np.sum(np.einsum("ij,ij->i", tri[:, 0], cross)) / 6
    return {"vertex_count": len(vertices), "triangle_count": len(faces),
            "watertight": bool(np.all(counts == 2) and np.all(area > 0) and np.all(np.isfinite(vertices))),
            "boundary_edge_count": int(np.count_nonzero(counts == 1)),
            "nonmanifold_edge_count": int(np.count_nonzero(counts > 2)),
            "zero_area_triangle_count": int(np.count_nonzero(area <= 0)),
            "signed_volume_nm3": float(volume)}


def write_stl(geometry, path, y_stride=4, z_samples=9):
    """Write binary STL in micrometres; STL itself has no material or unit field."""
    vertices, faces = triangulate_ridges(geometry, y_stride, z_samples)
    quality = mesh_quality(vertices, faces)
    if not quality["watertight"] or quality["signed_volume_nm3"] <= 0:
        raise ValueError(f"Invalid ridge mesh: {quality}")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    triangles = vertices[faces] / 1000.0
    normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    normals /= np.linalg.norm(normals, axis=1)[:, None]
    with path.open("wb") as handle:
        handle.write(b"grating3d closed ridge mesh; coordinates in micrometres".ljust(80, b"\0"))
        handle.write(struct.pack("<I", len(faces)))
        for normal, triangle in zip(normals, triangles):
            handle.write(struct.pack("<12fH", *normal, *triangle.ravel(), 0))
    return {"path": str(path), "coordinate_unit": "um", **quality}


def plot_preview(geometry, path):
    """Write a 3D preview in micrometres and return its local path."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    vertices, faces = triangulate_ridges(geometry, y_stride=8, z_samples=7)
    vertices = vertices / 1000
    fig = plt.figure(figsize=(12, 7))
    ax = fig.add_subplot(111, projection="3d")
    collection = Poly3DCollection(vertices[faces], facecolor="#4aaec8", edgecolor="none", alpha=0.95)
    ax.add_collection3d(collection)
    bounds = [geometry[key] for key in ("x_bounds_nm", "y_bounds_nm", "z_bounds_nm")]
    ax.set_xlim(np.asarray(bounds[0]) / 1000)
    ax.set_ylim(np.asarray(bounds[1]) / 1000)
    ax.set_zlim(np.asarray(bounds[2]) / 1000)
    ax.set_zticks(np.linspace(bounds[2][0] / 1000, bounds[2][1] / 1000, 3))
    ax.set_box_aspect([np.ptp(bounds[0]), np.ptp(bounds[1]), max(np.ptp(bounds[2]), 0.1)])
    ax.set_xlabel("x (um)"); ax.set_ylabel("y (um)"); ax.set_zlabel("z (um)")
    ax.view_init(elev=28, azim=-54)
    ax.set_title("SEM + TEM parametric ridge reconstruction")
    fig.tight_layout()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return str(path)
