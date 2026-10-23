"""Read-only, step-based presentation shared by the table and geometry plots.

A process step can produce many disjoint solids after Boolean partitioning.
Those pieces retain a single origin in the user interface. Old exchange files
are never rewritten: an unambiguous recorded step name may recover the origin,
otherwise the display says that provenance is unknown.
"""
from __future__ import annotations

import re


STEP_COLORS = (
    "#4c78a8", "#f58518", "#54a24b", "#b279a2", "#e45756",
    "#72b7b2", "#b89b33", "#9d755d", "#797ac0", "#d66fa3",
)


def step_groups(geometry):
    """Return one display record per recorded process step, without mutation.

    Records expose key/id/index/name/label/color/solids/operation/enabled/status,
    material/count/z_min_um/z_max_um and source_known/source_method. ``solids``
    contains references to the input solids; consumers must treat them as
    read-only. A final unknown group is present only when provenance is missing.
    """
    recipe = geometry.get("provenance", {}).get("recipe", {})
    declared = recipe.get("steps", [])
    history = geometry.get("process_history", [])
    steps = declared if declared else history
    groups = []
    for index, step in enumerate(steps):
        step_id = step.get("id")
        name = step.get("name") or step_id or f"步骤 {index + 1}"
        groups.append({
            "key": f"step:{index}:{step_id or ''}", "id": step_id,
            "index": index, "name": name, "label": f"{index + 1:02d}  {name}",
            "color": STEP_COLORS[index % len(STEP_COLORS)], "solids": [],
            "operation": step.get("operation", ""), "enabled": step.get("enabled", True),
            "material": step.get("material") or ", ".join(step.get("target_materials", [])),
            "source_known": True, "source_method": "recorded_process_step",
        })

    def match_origin(solid):
        # New solids have explicit identity; do not reinterpret a broken ID as
        # another step merely because a display name happens to match.
        if solid.get("source_step_id") is not None:
            matches = [g for g in groups if g["id"] == solid["source_step_id"]]
            return (matches[0], "explicit_step_id") if len(matches) == 1 else (None, "unknown")
        name = solid.get("name", "")
        matches = []
        for group, step in zip(groups, steps):
            # Historical _rewrite named pieces from step.name, falling back
            # to step.id. A unique exact name_<integer> is reliable evidence.
            recorded_name = step.get("name") or step.get("id")
            if recorded_name and re.fullmatch(re.escape(str(recorded_name)) + r"_\d+", name):
                matches.append(group)
        return (matches[0], "legacy_unique_name") if len(matches) == 1 else (None, "unknown")

    unknown = None
    for solid in geometry.get("solids", []):
        group, method = match_origin(solid)
        if group is None:
            if unknown is None:
                unknown = {
                    "key": "unknown", "id": None, "index": None,
                    "name": "来源未记录", "label": "来源未记录",
                    "color": "#777777", "solids": [], "operation": "",
                    "enabled": True, "material": "", "source_known": False,
                    "source_method": "unknown",
                }
            group = unknown
        elif method == "legacy_unique_name":
            group["source_method"] = method
        elif group["source_method"] != "legacy_unique_name":
            group["source_method"] = method
        group["solids"].append(solid)
    if unknown is not None:
        groups.append(unknown)

    executed = {entry.get("id") for entry in history}
    for group in groups:
        solids = group["solids"]
        group["count"] = len(solids)
        group["z_min_um"] = min((s["z_min_um"] for s in solids), default=None)
        group["z_max_um"] = max((s["z_max_um"] for s in solids), default=None)
        if solids:
            group["material"] = ", ".join(sorted({s["material"] for s in solids}))
        if not group["source_known"]:
            group["status"] = "历史来源未知"
        elif not group["enabled"]:
            group["status"] = "停用"
        elif geometry.get("snapshot_step_id") and group["id"] not in executed:
            group["status"] = "快照尚未执行"
        elif group["operation"] in ("etch", "delete"):
            group["status"] = "移除材料步骤"
        elif not solids:
            group["status"] = "无剩余实体"
        else:
            group["status"] = "已生成" if group["source_method"] != "legacy_unique_name" else "已生成（历史名称匹配）"
    return groups
