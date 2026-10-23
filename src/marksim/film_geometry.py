"""Surface-attached film geometry, independent of deposition kinetics.

Supported input is a vertical, single-valued height surface: at every xy point
the occupied volume is continuous from a supported common bottom to h(x, y).
Background-labelled solids count as empty space. No undercut, suspended bridge,
enclosed void, or isolated island without substrate support is accepted.

``conformal`` is a *square-corner geometric approximation*: union the occupied
volume, extend upwards by ``thickness_um``, offset its xy slices outwards by
``sidewall_thickness_um`` with miter joins, then subtract the original volume.
This coats exposed tops, trench floors and sidewalls, never buried interfaces.
The two thicknesses are independent. Narrow trenches can close geometrically;
this is not an ALD/CVD growth or pinch-off/seam model.

``planar_fill`` fills empty space up to max(h) + thickness_um. Neither operation
changes the material or volume of any existing non-background solid.
"""
from __future__ import annotations

import math


FILM_MODES = ("absolute", "conformal", "planar_fill")
FILM_MODEL_VERSION = "supported-height-surface-film-1.0"


def _ops():
    # Lazy import keeps the public geometry module usable without an import cycle.
    from . import geometry
    return geometry


def _area(polygons):
    return sum(p.area() for p in polygons)


def _union_at(solids, z, *, grow_top=0.0):
    g = _ops()
    polygons = [g._gds().Polygon(s["polygon_xy_um"]) for s in solids
                if s["z_min_um"] < z < s["z_max_um"] + grow_top]
    return g._boolean(polygons, [], "or")


def _levels(values):
    """Keep exact z interfaces: polygon Boolean precision applies only to xy.

    Snapping z would move a new film off an unchanged original interface,
    creating a gap or overlap even though all xy polygon operations succeeded.
    """
    return sorted({float(v) for v in values})


def _supported_surface(solids, recipe):
    """Check continuous support using exact polygon differences at every slab."""
    g = _ops()
    occupied = [s for s in solids if s["material"] != recipe["background_material"]]
    if not occupied:
        raise ValueError("薄膜没有可依附的实体：请先创建衬底 / film requires a supported substrate")
    bounds = recipe["cell_bounds_um"]
    clip = [g._rect(bounds)]
    tolerance = max(g.PRECISION_UM ** 2, _area(clip) * 1e-10)
    levels = _levels(v for s in occupied for v in (s["z_min_um"], s["z_max_um"]))
    bottom = levels[0]
    # Meep's substrate extension applies only to a solid touching this floor.
    # A slab at greater z must not silently be assumed to extend underneath.
    if bottom > bounds[2][0] + g.PRECISION_UM:
        raise ValueError("薄膜下方实体未接触单元底面 / unsupported floating base; create a full-cell substrate touching cell z_min")
    previous = clip
    for lo, hi in zip(levels, levels[1:]):
        current = _union_at(occupied, (lo + hi) / 2)
        if lo == bottom and _area(g._boolean(clip, current, "not")) > tolerance:
            raise ValueError("薄膜要求完整衬底覆盖xy单元 / no full-cell support")
        # The sets U(z) of occupied xy points must be nested as z increases.
        # Any new area at greater z indicates an overhang or an unsupported gap.
        if _area(g._boolean(current, previous, "not")) > tolerance:
            raise ValueError("薄膜暂不支持悬空、下切或内部空洞；请先修正下方结构 / unsupported non-single-valued surface")
        previous = current
    return occupied, levels, clip, tolerance


def surface_film(solids, recipe, step):
    """Return non-overlapping *new* film slabs and auditable construction rules."""
    g = _ops()
    mode = step.get("film_mode", "absolute")
    if mode not in ("conformal", "planar_fill"):
        raise ValueError("surface_film requires conformal or planar_fill film_mode")
    if step["material"] == recipe["background_material"]:
        raise ValueError("贴合/填平薄膜不能使用背景材料 / film material must differ from background")
    if step.get("mask") is not None or step.get("mask_polarity", "positive") != "positive":
        raise ValueError("贴合/填平薄膜覆盖整个表面，不接受掩模 / surface film does not accept a mask")
    for key in ("cd_bias_um", "delta_x_um", "delta_y_um", "rotation_deg"):
        if g._finite(step.get(key, 0), key) != 0:
            raise ValueError(f"表面薄膜不接受 {key}；请使用全表面工艺 / unsupported surface film transform")
    thickness = g._finite(step.get("thickness_um"), "thickness_um")
    if thickness < g.PRECISION_UM:
        raise ValueError("thickness_um must be positive and at least the geometry precision (1e-8 um)")
    lateral = 0.0
    if mode == "conformal":
        if "sidewall_thickness_um" not in step:
            raise ValueError("等厚包覆需要显式指定侧壁厚度 sidewall_thickness_um")
        lateral = g._finite(step["sidewall_thickness_um"], "sidewall_thickness_um")
        if lateral < 0:
            raise ValueError("sidewall_thickness_um must be nonnegative")
    occupied, old_levels, clip, tolerance = _supported_surface(solids, recipe)
    top = old_levels[-1] + thickness
    if not math.isfinite(top) or top > recipe["cell_bounds_um"][2][1] + g.PRECISION_UM:
        raise ValueError("薄膜超出明确设置的几何z范围 / film exceeds explicitly declared geometry z bounds")
    events = _levels([*old_levels, top] if mode == "planar_fill"
                     else [*old_levels, *(z + thickness for z in old_levels)])
    result = []
    for lo, hi in zip(events, events[1:]):
        if hi <= lo:
            continue
        middle = (lo + hi) / 2
        original = _union_at(occupied, middle)
        if mode == "planar_fill":
            grown = clip
        else:
            grown = _union_at(occupied, middle, grow_top=thickness)
            if lateral and grown:
                grown = g._gds().offset(grown, lateral, join="miter",
                                       precision=g.PRECISION_UM, use_union=True)
            # Offset at a cell seam must see the neighboring periodic image,
            # including at large thickness; wrap before subtracting originals.
            grown = g.wrap_polygons(grown, recipe["cell_bounds_um"], recipe["period_x_um"],
                                    period_y_um=recipe.get("period_y_um"))
        film = g._simple(g._boolean(grown, original, "not"))
        for polygon in film:
            if polygon.area() > tolerance:
                result.append({"polygon_xy_um": polygon.points.tolist(), "z_min_um": lo, "z_max_um": hi})
    if not result:
        raise ValueError("薄膜在几何精度下没有有效体积 / film has no resolvable volume")
    return result, {
        "film_mode": mode, "film_model_version": FILM_MODEL_VERSION,
        "thickness_um": thickness,
        "sidewall_thickness_um": lateral if mode == "conformal" else None,
        "reference_top_z_um": old_levels[-1], "result_top_z_um": top,
        "rule": ("square_corner_surface_growth_minus_original" if mode == "conformal"
                 else "fill_empty_volume_to_highest_surface_plus_thickness"),
        "surface_validation": "full-cell supported single-valued height surface; background excluded",
        "limitations": "Geometric model only, 90-degree vertical walls and miter corners; no deposition dynamics or pinch-off model.",
        "z_reference": "Actual existing surface; absolute z_min_um/z_max_um are not used in this mode",
    }
