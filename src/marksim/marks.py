"""Parameterized public AH mark geometry; independent of Tk and Meep.

The family digit specifies the internal harmonic, not the simulation period.
P is the mother period; p=P/h is stripe centre pitch. CD is the nominal GDS
linewidth, before any separate process CD bias. See docs/mark_types.md.
"""
from __future__ import annotations

import copy
import math
from pathlib import Path
import uuid

from .schemas import file_hash
from .paths import resolve_input_path

TEMPLATE_VERSION = "ah-public-stripes-1.0"
_FAMILIES = {"AH32": (3, 2), "AH53": (5, 3), "AH74": (7, 4)}
_DERIVED = {"subpitch_um", "target_order", "stripe_count", "centers_um",
            "occupied_width_um", "fill_fraction", "template_version"}
_BASE = {"schema_version", "mark_type", "mother_period_um", "cd_um", "height_um"}
_BINDING = {"target_step_id", "template_mask"}


def catalog():
    return [{"id": name, "label": name, "status": "implemented", "target_order": h,
             "stripe_count": n} for name, (h, n) in _FAMILIES.items()] + [
        {"id": "OTHER_RESERVED", "label": "其他 Mark（预留，尚未实现）", "status": "reserved",
         "target_order": None, "stripe_count": None}]


def default_mark_config(mark_type="AH53"):
    if mark_type not in _FAMILIES:
        raise NotImplementedError(f"Mark 类型尚未实现：{mark_type}")
    harmonic, _ = _FAMILIES[mark_type]
    return {"schema_version": "1.0", "mark_type": mark_type, "mother_period_um": 16.,
            "cd_um": 8. / harmonic, "height_um": .3}


def _positive(value, field):
    if isinstance(value, bool): raise ValueError(f"{field} 必须是正数")
    try: number = float(value)
    except (TypeError, ValueError): raise ValueError(f"{field} 必须是有限正数") from None
    if not math.isfinite(number) or number <= 0: raise ValueError(f"{field} 必须是有限正数")
    return number


def normalize_mark_config(config):
    """Validate base dimensions and reject stale derived fields in saved JSON."""
    if not isinstance(config, dict): raise ValueError("Mark 配置必须是对象")
    unknown = set(config) - _BASE - _DERIVED - _BINDING
    if unknown: raise ValueError(f"未知 Mark 参数：{sorted(unknown)}")
    if config.get("schema_version", "1.0") != "1.0": raise ValueError("Unsupported Mark schema_version")
    name = config.get("mark_type", "AH53")
    defaults = default_mark_config(name)
    h, count = _FAMILIES[name]
    result = {**defaults, **{key: config[key] for key in _BASE if key in config}}
    for key in ("mother_period_um", "cd_um", "height_um"):
        result[key] = _positive(result[key], key)
    period, cd = result["mother_period_um"], result["cd_um"]
    pitch = period / h
    if cd >= pitch: raise ValueError("CD 必须小于条纹子周期，避免相邻条纹重叠")
    # Guard collapsed features and catastrophic coordinates before gdstk.
    if min(cd, pitch-cd, result["height_um"]) < 1e-6 or period > 1e6:
        raise ValueError("Mark 尺寸超出当前 GDS 精度 / 数值范围")
    derived = {"subpitch_um": pitch, "stripe_count": count, "target_order": h,
               "centers_um": [(i - (count-1)/2) * pitch for i in range(count)],
               "occupied_width_um": (count-1)*pitch + cd,
               "fill_fraction": count*cd/period, "template_version": TEMPLATE_VERSION}
    for key in _DERIVED & set(config):
        expected, actual = derived[key], config[key]
        if isinstance(expected, (float, list)):
            import numpy as np
            try: equal = np.shape(actual) == np.shape(expected) and np.allclose(actual, expected, rtol=1e-10, atol=1e-10)
            except (TypeError, ValueError): equal = False
        else: equal = actual == expected
        if not equal: raise ValueError(f"Mark 派生参数 {key} 与类型 / 母周期 / CD 不一致")
    result.update(derived)
    result.update({key: copy.deepcopy(config[key]) for key in _BINDING if key in config})
    return result


def _geometry_mark(config):
    return normalize_mark_config(config)


def write_mark_gds(path, config, *, y_bounds_um=(-1., 1.)):
    """Write a NEW source mask with full y-invariant stripes, never replace one."""
    import gdstk
    path = Path(path)
    if path.exists(): raise FileExistsError(f"保留已有 GDS，不覆盖：{path}")
    cfg = normalize_mark_config(config)
    y0, y1 = map(float, y_bounds_um)
    if not math.isfinite(y0) or not math.isfinite(y1) or y1 <= y0:
        raise ValueError("y 边界必须有限且递增")
    extent = max(cfg["mother_period_um"], abs(y0), abs(y1), 1.)
    precision = max(1e-12, extent*1e-6/1e9)  # keep all database coordinates in int32
    grid_um = precision/1e-6
    if min(cfg["cd_um"], cfg["subpitch_um"]-cfg["cd_um"], y1-y0) < 4*grid_um:
        raise ValueError("Mark CD / 间隙 / 条纹范围小于当前 GDS 网格的四倍，无法可靠表示")
    library = gdstk.Library(name=cfg["mark_type"]+"_PARAMETRIC", unit=1e-6, precision=precision)
    cell_name = cfg["mark_type"]+"_MARK"
    cell = library.new_cell(cell_name)
    for x in cfg["centers_um"]:
        cell.add(gdstk.rectangle((x-cfg["cd_um"]/2, y0), (x+cfg["cd_um"]/2, y1), layer=1, datatype=0))
    path.parent.mkdir(parents=True, exist_ok=True)
    library.write_gds(path)
    return {"sha256": file_hash(path), "cell": cell_name, "layer": 1, "datatype": 0,
            "y_bounds_um": [y0, y1]}


def validate_recipe_mark(recipe, base_dir):
    """Bind a declared template to its actual source mask, not a name alone.

    Subsequent process CD bias, height and layer shifts remain legitimate
    perturbations recorded by the ordinary process history. This declaration
    describes the nominal input mask, not a promise of optical performance.
    """
    if "mark" not in recipe: return None
    cfg = _geometry_mark(recipe["mark"])
    if not math.isclose(float(recipe["period_x_um"]), cfg["mother_period_um"], rel_tol=1e-10):
        raise ValueError("Mark 母周期与工艺计算单元不一致")
    matches = [s for s in recipe["steps"] if s.get("id") == cfg.get("target_step_id")]
    if len(matches) != 1: raise ValueError("Mark 需要唯一的目标掩模步骤 target_step_id")
    step = matches[0]
    if step.get("operation") not in ("add", "etch") or not step.get("enabled", True):
        raise ValueError("Mark 目标步骤必须是已启用的 add / etch")
    binding = cfg.get("template_mask")
    if not isinstance(binding, dict): raise ValueError("Mark 缺少生成 GDS 的 template_mask 来源记录")
    mask = step.get("mask", {})
    if any(mask.get(key) != binding.get(key) for key in ("cell", "layer", "datatype")):
        raise ValueError("Mark 模板与当前 GDS cell / layer / datatype 不一致")
    if mask.get("roi_um") is not None: raise ValueError("参数化 Mark 不允许额外 ROI 裁切；请另存为自定义 GDS 分析")
    path = resolve_input_path(mask.get("gds_path", ""), base_dir=base_dir)
    if not path.is_file() or file_hash(path) != binding.get("sha256"):
        raise ValueError("Mark 模板 GDS 文件已改变或缺失；请重新应用 Mark 参数")
    import numpy as np
    if not np.allclose(recipe["cell_bounds_um"][1], binding.get("y_bounds_um"), rtol=0, atol=1e-10):
        raise ValueError("Mark 模板的条纹方向范围与计算单元不一致")
    from .geometry import read_gds_polygons
    polygons, source = read_gds_polygons(path, cell=mask["cell"], layer=mask["layer"], datatype=mask["datatype"])
    if len(polygons) != cfg["stripe_count"]: raise ValueError("Mark 条数与实际 GDS 不一致")
    tolerance = max(2e-8, 2*source["gds_precision_m"]/1e-6)
    polygons = sorted(polygons, key=lambda poly: poly.bounding_box()[0][0])
    y0, y1 = binding["y_bounds_um"]
    for x, poly in zip(cfg["centers_um"], polygons):
        expected = [[x-cfg["cd_um"]/2, y0], [x+cfg["cd_um"]/2, y1]]
        box = np.asarray(poly.bounding_box())
        area = float(np.prod(box[1]-box[0]))
        if not np.allclose(box, expected, rtol=0, atol=tolerance) or not math.isclose(poly.area(), area, abs_tol=tolerance, rel_tol=1e-8):
            raise ValueError("Mark 的 pitch / CD / 条纹矩形与实际 GDS 不一致")
    return cfg


def apply_mark_to_project(data, config, *, target_step_id=None):
    """Apply to a copy; only a new generated mask is written, no project saves.

    The user explicitly selects the patterned process step. Other materials and
    process steps are retained. No optical readout configuration is created.
    """
    cfg = normalize_mark_config(config)
    result = copy.deepcopy(data)
    recipe = result["process"]
    if not recipe.get("invariant_y", False): raise ValueError("当前参数化 AH 模板要求沿条纹方向均匀")
    target_step_id = target_step_id or recipe.get("mark", {}).get("target_step_id")
    if target_step_id is None:
        choices = [s for s in recipe["steps"] if s.get("operation") in ("add", "etch") and s.get("mask")]
        if len(choices) != 1: raise ValueError("请明确选择一个 add / etch 掩模步骤，不能猜测要替换的层")
        target_step_id = choices[0]["id"]
    matches = [s for s in recipe["steps"] if s.get("id") == target_step_id]
    if len(matches) != 1 or matches[0].get("operation") not in ("add", "etch"):
        raise ValueError("目标步骤必须是唯一的 add / etch 掩模步骤")
    step = matches[0]
    cfg["target_step_id"] = target_step_id
    # Validate everything before creating the reversible new source artifact.
    recipe["period_x_um"] = cfg["mother_period_um"]
    recipe["cell_bounds_um"][0] = [-cfg["mother_period_um"]/2, cfg["mother_period_um"]/2]
    result["simulation"]["period_x_um"] = cfg["mother_period_um"]
    relative = Path("inputs")/"mark_variants"/(cfg["mark_type"]+"_"+uuid.uuid4().hex+".gds")
    cfg["template_mask"] = write_mark_gds(Path(result["folder"])/relative, cfg,
                                         y_bounds_um=recipe["cell_bounds_um"][1])
    step["mask"] = {"gds_path": relative.as_posix(), **{key: cfg["template_mask"][key] for key in ("cell", "layer", "datatype")}}
    step.update(mask_polarity="positive", cd_bias_um=0., delta_x_um=0., delta_y_um=0., rotation_deg=0.,
                enabled=True, sidewall_angle_deg=90., name=cfg["mark_type"]+" patterned layer")
    if step["operation"] == "etch": step["etch_depth_um"] = cfg["height_um"]
    else:
        step.pop("z_max_um", None)
        step["thickness_um"] = cfg["height_um"]
    recipe["mark"] = _geometry_mark(cfg)
    result["project"].pop("ah53_geometry", None)
    return result


def create_mark_project(folder, mark_type="AH53", *, parameters=None, previews=True):
    """Create a new complete parameterized demo project, using the shared builder."""
    cfg = normalize_mark_config({**default_mark_config(mark_type), **(parameters or {}), "mark_type": mark_type})
    from .examples import _create_demo_project
    return _create_demo_project(folder, previews=previews, mark_config=cfg)
