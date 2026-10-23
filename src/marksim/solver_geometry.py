"""Exact rectangle union used only by the periodic 2D solver adapter.

Process geometry deliberately retains individual steps and their provenance.
Touching pieces of the same material are united where they form rectangles:
subpixel averaging at artificial object edges can introduce erroneous layers
and spurious diffraction even when the physical structure is a uniform film.
"""
from __future__ import annotations

from collections import defaultdict
import math


RECTANGLE_MERGE_VERSION = "same-material-exact-union-1.0"


def lateral_uniformity(geometry):
    """Whether every material layer is constant across the full x period.

    This is a geometric diagnostic only. It does not threshold, suppress or
    replace any computed diffraction amplitude. Empty background is uniform.
    """
    xlo, xhi = map(float, geometry["cell_bounds_um"][0])
    return all(rect["x_min_um"] == xlo and rect["x_max_um"] == xhi
               for rect in canonical_rectangles(geometry))


def canonical_rectangles(geometry, *, simulation_z_min_um=None):
    """Return non-overlapping solver rectangles without changing ``geometry``.

    Each result contains ``material``, ``x_min_um``, ``x_max_um``, ``z_min_um``
    and ``z_max_um``. Inputs must be validated, rectangular, y-invariant solids.
    Only identical material IDs are merged, never merely similar n/k values.

    Every original z interface is preserved exactly. Within each z slice,
    touching x intervals are united; identical intervals in successive slices
    are then joined vertically. There is no geometric tolerance in these
    unions, so a real gap, however small, is not silently filled.

    If a simulation lower boundary is supplied, the existing substrate
    continuation is applied before union. A substrate must actually touch the
    geometry's lower boundary to be continued into the bottom PML.
    """
    if not geometry.get("invariant_y"):
        raise ValueError("Solver rectangle union requires verified y invariance")
    bounds = geometry["cell_bounds_um"]
    bottom = float(bounds[2][0])
    pieces = []
    for solid in geometry["solids"]:
        polygon = solid["polygon_xy_um"]
        xs = [float(point[0]) for point in polygon]
        ys = [float(point[1]) for point in polygon]
        xlo, xhi, ylo, yhi = min(xs), max(xs), min(ys), max(ys)
        # Reject unsuitable inputs when this helper is used outside validate_task.
        area = abs(sum(xs[i] * ys[(i + 1) % len(xs)]
                       - ys[i] * xs[(i + 1) % len(xs)]
                       for i in range(len(xs)))) / 2
        if not math.isclose(area, (xhi - xlo) * (yhi - ylo),
                            rel_tol=1e-8, abs_tol=1e-8):
            raise ValueError("Solver rectangle union requires exact rectangles")
        if not (math.isclose(ylo, bounds[1][0], rel_tol=0, abs_tol=1e-7)
                and math.isclose(yhi, bounds[1][1], rel_tol=0, abs_tol=1e-7)):
            raise ValueError("Solver rectangles must span the complete y cell")
        zlo, zhi = float(solid["z_min_um"]), float(solid["z_max_um"])
        if (simulation_z_min_um is not None
                and geometry.get("substrate_extend_bottom", True)
                and solid["material"] == geometry.get("substrate_material")
                and zlo == bottom):
            zlo = min(zlo, float(simulation_z_min_um))
        if not all(math.isfinite(v) for v in (xlo, xhi, zlo, zhi)):
            raise ValueError("Solver rectangle contains non-finite coordinates")
        if xhi <= xlo or zhi <= zlo:
            raise ValueError("Solver rectangle must have positive area")
        pieces.append({"material": solid["material"], "x_min_um": xlo,
                       "x_max_um": xhi, "z_min_um": zlo, "z_max_um": zhi})

    levels = sorted({r[key] for r in pieces for key in ("z_min_um", "z_max_um")})
    result = []
    previous = {}
    for zlo, zhi in zip(levels, levels[1:]):
        by_material = defaultdict(list)
        for rect in pieces:
            if rect["z_min_um"] <= zlo and rect["z_max_um"] >= zhi:
                by_material[rect["material"]].append(
                    (rect["x_min_um"], rect["x_max_um"]))
        current = {}
        for material in sorted(by_material):
            intervals = []
            for xlo, xhi in sorted(by_material[material]):
                if intervals and xlo <= intervals[-1][1]:
                    intervals[-1] = (intervals[-1][0], max(intervals[-1][1], xhi))
                else:
                    intervals.append((xlo, xhi))
            for xlo, xhi in intervals:
                key = (material, xlo, xhi)
                if key in previous and previous[key]["z_max_um"] == zlo:
                    rect = previous[key]
                    rect["z_max_um"] = zhi
                else:
                    rect = {"material": material, "x_min_um": xlo, "x_max_um": xhi,
                            "z_min_um": zlo, "z_max_um": zhi}
                    result.append(rect)
                current[key] = rect
        previous = current
    return sorted(result, key=lambda r: (r["z_min_um"], r["z_max_um"],
                                         r["x_min_um"], r["material"]))
