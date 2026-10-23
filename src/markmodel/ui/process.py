"""Explicit Mark and process forms, including inspection of actual GDS cells."""
from __future__ import annotations

import copy
import math
from pathlib import Path
import shutil
import tkinter as tk
from tkinter import filedialog, ttk
from uuid import uuid4

from .widgets import JsonDialog, card, field, hint, make_modal

OPS = {"创建衬底": "create_substrate", "按掩模添加": "add", "垂直刻蚀": "etch", "贴合表面薄膜": "create_film", "删除材料": "delete"}
FILMS = {"等厚包覆（顶部 + 侧壁）": "conformal", "填平覆盖（平坦顶面）": "planar_fill", "指定绝对高度（高级）": "absolute"}


def inspect_gds_choices(path):
    """Read cells and actual polygon specs, including referenced cells."""
    import gdstk
    library = gdstk.read_gds(str(path))
    return {"unit_m": library.unit, "precision_m": library.precision,
            "cells": {cell.name: sorted({(p.layer, p.datatype) for p in cell.get_polygons()}) for cell in library.cells}}


class ProcessPage:
    def create_process_page(self, page):
        self.step_index = None
        self.step_dirty = False
        self._filling_step = False
        self.mark_dirty = False
        self._filling_mark = False
        self._mark_fields_changed = set()
        self.mark_vars = {key: tk.StringVar(value=value) for key, value in
                          {"mark_type": "AH53", "mother_period_um": "16", "cd_nm": "1600", "height_nm": "300", "target_step_id": "ridge"}.items()}
        box = card(page, "公共 AH 条纹模板")
        self.mark_combos = {}
        for col, (key, label, values) in enumerate((
            ("mark_type", "Mark 类型", ["AH32", "AH53", "AH74"]),
            ("mother_period_um", "母周期 L · µm", None), ("cd_nm", "单条纹 CD · nm", None),
            ("height_nm", "模板高度 / 刻蚀深度 · nm", None), ("target_step_id", "绑定工艺步骤 ID", []))):
            self.mark_combos[key] = field(box, 0, col, label, self.mark_vars[key], values=values, width=15)
        self.mark_note = tk.StringVar(value="子 pitch = 母周期 / 目标阶次；AH32/53/74 分别为 3/5/7。CD 是单条纹宽度。")
        ttk.Label(box, textvariable=self.mark_note, style="Hint.TLabel", wraplength=1100).grid(row=1, column=0, columnspan=5, sticky="w", pady=8)
        bar = ttk.Frame(box)
        bar.grid(row=2, column=0, columnspan=5, sticky="ew")
        self.mark_apply_button = self.button(bar, "应用 Mark 尺寸", self.apply_mark, primary=True)
        self.button(bar, "按所选类型恢复模板默认值", self.reset_mark_form)
        self.button(bar, "高级：计算单元 / 完整工艺", self.edit_recipe)
        for key, variable in self.mark_vars.items():
            variable.trace_add("write", lambda *_, key=key: self._mark_edited(key))
        hint_box = ttk.Label(box, text="修改尺寸后，保存或生成也会自动应用。CD 是名义掩模线宽；工艺 CD 偏置与平移保持独立。", style="Hint.TLabel", wraplength=1050)
        hint_box.grid(row=3, column=0, columnspan=5, sticky="w", pady=(2, 5))
        step_box = card(page, "工艺顺序 · 后一步在前一步表面执行")
        bar = ttk.Frame(step_box)
        bar.pack(fill="x")
        self.step_actions = {}
        for title, command in (("＋ 新增薄膜", self.add_step), ("删除步骤", self.delete_step),
                               ("上移", lambda: self.move_step(-1)), ("下移", lambda: self.move_step(1))):
            self.step_actions[title] = self.button(bar, title, command)
        self.step_tree = ttk.Treeview(step_box, columns=("operation", "material", "enabled"), show="tree headings", height=6, selectmode="browse")
        for key, label, width in (("#0", "步骤 / 名称", 320), ("operation", "操作", 200), ("material", "材料", 240), ("enabled", "状态", 90)):
            self.step_tree.heading(key, text=label)
            self.step_tree.column(key, width=width, minwidth=70)
        self.step_tree.pack(fill="x", pady=8)
        self.step_tree.bind("<<TreeviewSelect>>", self.select_step)
        editor = ttk.Frame(step_box)
        editor.pack(fill="x")
        specs = [
            ("name", "步骤名称", None), ("operation", "几何操作", list(OPS)), ("material", "材料（刻蚀/删除可用逗号分隔）", []),
            ("z", "z 起点 / 刻蚀参考面 · nm", None), ("thickness", "顶部厚度 / 删除范围 · nm", None), ("film_mode", "薄膜形貌", list(FILMS)),
            ("sidewall", "包覆侧壁厚度 · nm", None), ("etch_depth", "刻蚀深度 · nm", None), ("cd_bias", "CD 总偏置 · nm", None),
            ("dx", "Δx · nm", None), ("dy", "Δy · nm", None), ("rotation", "旋转角 · deg", None)]
        self.step_vars = {key: tk.StringVar() for key, _, _ in specs}
        self.step_controls = {}
        for i, (key, label, values) in enumerate(specs):
            self.step_controls[key] = field(editor, i // 3, i % 3, label, self.step_vars[key], values=values)
            if key == "material":
                self.step_controls[key].configure(state="normal")
            self.step_vars[key].trace_add("write", self._step_edited)
        self.step_enabled = tk.BooleanVar(value=True)
        self.step_enabled.trace_add("write", self._step_edited)
        bar = ttk.Frame(step_box)
        bar.pack(fill="x", pady=10)
        self.step_enabled_control = ttk.Checkbutton(bar, text="启用此步骤", variable=self.step_enabled)
        self.step_enabled_control.pack(side="left", padx=(0, 15))
        self.step_actions["应用"] = self.button(bar, "应用步骤修改", self.apply_step, primary=True)
        self.step_actions["GDS"] = self.button(bar, "选取实际 GDS / cell / 图层…", self.gds_dialog)
        self.step_actions["JSON"] = self.button(bar, "高级：此步骤 JSON", self.edit_step_json)
        self.step_hint = tk.StringVar(value="选中步骤后编辑；保存或生成前也会应用当前步骤表单。")
        ttk.Label(step_box, textvariable=self.step_hint, style="Hint.TLabel", wraplength=1050).pack(fill="x")
        hint(step_box, "包覆膜贴合已有顶面、沟槽底面和侧壁，侧壁厚度独立填写；填平膜从已有表面填充至最高面加顶部厚度。垂直侧壁几何是当前工艺模型假设。")
        self._update_step_controls()

    def _mark_edited(self, key):
        if self._filling_mark or not self.data:
            return
        self._mark_fields_changed.add(key)
        self.mark_dirty = True
        self._input_revision += 1
        self.model = None
        self.geometry_note.set("Mark 尺寸有未应用修改；保存 / 生成时会应用，之前的结构不可导出。")
        self.project_label.set(str(self.data["folder"]) + "  • Mark 尺寸待应用")
        if hasattr(self, "_update_buttons"):
            self._update_buttons()
        self._update_mark_note()

    def _update_mark_note(self):
        """Live dimensions are a form preview, never evidence of built geometry."""
        from marksim.marks import normalize_mark_config
        try:
            cfg = normalize_mark_config(self._mark_config())
            prefix = "待应用" if self.mark_dirty else "当前模板"
            self.mark_note.set(f"{prefix} {cfg['mark_type']} · 子 pitch {cfg['subpitch_um']:g} µm · "
                               f"{cfg['stripe_count']} 条纹 / 母周期 · CD {cfg['cd_um'] * 1000:g} nm。")
        except (TypeError, ValueError, NotImplementedError) as exc:
            self.mark_note.set("尺寸待修正：" + str(exc))

    def _mark_config(self):
        labels = {"mother_period_um": "母周期 L（µm）", "cd_nm": "单条纹 CD（nm）", "height_nm": "模板高度（nm）"}
        numbers = {}
        for key, label in labels.items():
            try:
                value = float(self.mark_vars[key].get())
            except ValueError:
                raise ValueError(f"{label} 请填写数字") from None
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{label} 必须是有限正数")
            numbers[key] = value
        return {"mark_type": self.mark_vars["mark_type"].get(), "mother_period_um": numbers["mother_period_um"],
                "cd_um": numbers["cd_nm"] / 1000, "height_um": numbers["height_nm"] / 1000}

    def commit_pending_inputs(self):
        """Commit both forms without allowing a step refresh to erase Mark edits."""
        if self.mark_dirty:
            from marksim.marks import normalize_mark_config
            normalize_mark_config(self._mark_config())  # fail before committing the other form
        if self.step_dirty:
            self.apply_step()
        if self.mark_dirty:
            self.apply_mark()

    def _step_edited(self, *_):
        if not self._filling_step and self.step_index is not None:
            self._input_revision += 1
            self.step_dirty = True
            self.model = None
            self.geometry_note.set("步骤表单有未应用修改；先应用 / 保存并重新生成结构。")
            self.project_label.set(str(self.data["folder"]) + "  • 步骤修改待应用")
            if hasattr(self, "_update_buttons"):
                self._update_buttons()
        self._update_step_controls()

    def _update_step_controls(self):
        if hasattr(self, "step_controls") and "operation" in self.step_vars:
            op = OPS.get(self.step_vars["operation"].get())
            film = FILMS.get(self.step_vars["film_mode"].get())
            active = {"name", "operation", "material"}
            if op in ("add", "etch"):
                active |= {"cd_bias", "dx", "dy", "rotation"}
            if op in ("create_substrate", "add", "delete") or (op == "create_film" and film == "absolute"):
                active |= {"z", "thickness"}
            if op == "etch":
                active |= {"z", "etch_depth"}
            if op == "create_film":
                active |= {"film_mode", "thickness"}
                if film == "conformal":
                    active.add("sidewall")
            if self.step_index is None:
                active.clear()
            for key, control in self.step_controls.items():
                control.configure(state=("readonly" if key in ("operation", "film_mode") else "normal") if key in active else "disabled")
            if hasattr(self, "step_enabled_control"):
                self.step_enabled_control.configure(state="normal" if self.step_index is not None else "disabled")
            for key, button in getattr(self, "step_actions", {}).items():
                enabled = self.step_index is not None
                count = len(self.data["process"]["steps"]) if self.data else 0
                if key == "＋ 新增薄膜": enabled = bool(self.data)
                if key == "GDS": enabled = enabled and op in ("add", "etch")
                if key == "上移": enabled = enabled and self.step_index > 0
                if key == "下移": enabled = enabled and self.step_index < count - 1
                button.configure(state="normal" if enabled else "disabled")

    def refresh_mark(self, *, force=False):
        if not self.data:
            return
        config = self.data["process"].get("mark")
        choices = [step["id"] for step in self.data["process"]["steps"] if step.get("operation") in ("add", "etch")]
        self.mark_combos["target_step_id"].configure(values=choices)
        self.mark_apply_button.configure(state="normal" if choices else "disabled")
        if self.mark_dirty and not force:
            self._update_mark_note()
            return
        self.mark_dirty = False
        self._mark_fields_changed.clear()
        self._filling_mark = True
        if config:
            for key in ("mark_type", "mother_period_um", "target_step_id"):
                self.mark_vars[key].set(str(config.get(key, "")))
            self.mark_vars["cd_nm"].set(f"{1000 * config['cd_um']:g}")
            self.mark_vars["height_nm"].set(f"{1000 * config['height_um']:g}")
            self.mark_note.set(f"当前绑定 {config.get('mark_type')}；子 pitch = {config.get('subpitch_um', '?')} µm；生成新 GDS 时保留原掩模。")
        else:
            self.mark_note.set("当前为自定义 GDS 工艺；应用模板会替换你明确选择的掩模步骤。")
            self.mark_vars["target_step_id"].set(choices[0] if choices else "")
        self._filling_mark = False

    def reset_mark_form(self):
        from marksim.marks import default_mark_config
        config = default_mark_config(self.mark_vars["mark_type"].get())
        self.mark_vars["mother_period_um"].set(str(config["mother_period_um"]))
        self.mark_vars["cd_nm"].set(f"{config['cd_um'] * 1000:g}")
        self.mark_vars["height_nm"].set(f"{config['height_um'] * 1000:g}")
        self.status.set("模板默认值已填入；应用、保存或生成时生效。工艺材料与偏置保持不变。")

    def apply_mark(self):
        from .. import service
        self.require_project()
        config = self._mark_config()
        from marksim.marks import normalize_mark_config
        normalize_mark_config(config)
        target = self.mark_vars["target_step_id"].get()
        if not target or not any(s.get("id") == target and s.get("operation") in ("add", "etch")
                                 for s in self.data["process"]["steps"]):
            raise ValueError("请选择有效的添加 / 刻蚀步骤作为 Mark 绑定目标")
        if self.step_dirty:
            self.apply_step()
        original = next(s for s in self.data["process"]["steps"] if s["id"] == target)
        updated = service.apply_mark_parameters(self.data["folder"], self.data["process"], config, target_step_id=target)
        step = next(s for s in updated["steps"] if s["id"] == target)
        # A nominal mask update must not erase separately entered process offsets.
        for key in ("cd_bias_um", "delta_x_um", "delta_y_um", "rotation_deg", "name"):
            if key in original:
                step[key] = copy.deepcopy(original[key])
        # When only pitch/CD changed, preserve an independently adjusted height.
        # An explicit template-height edit or target/type change owns the height.
        if self.mark_dirty and not self._mark_fields_changed.intersection({"height_nm", "target_step_id", "mark_type"}):
            for key in ("thickness_um", "z_max_um", "etch_depth_um"):
                if key in original:
                    step[key] = original[key]
        self.data["process"] = updated
        self.mark_dirty = False
        self.refresh_steps()
        self.refresh_mark(force=True)
        self.changed()
        self.status.set("Mark 尺寸已应用；保留原 GDS、材料和工艺偏置。生成结构后可导出。")

    def refresh_steps(self, select=None):
        previous = self.step_index
        self.step_index = None
        self.step_dirty = False
        self.step_tree.delete(*self.step_tree.get_children())
        if not self.data:
            self.clear_step_form()
            return
        for index, step in enumerate(self.data["process"]["steps"]):
            self.step_tree.insert("", "end", iid=str(index), text=f"{index + 1:02d}  {step.get('name', step['id'])}",
                                  values=(next((k for k, v in OPS.items() if v == step["operation"]), step["operation"]),
                                          step.get("material", ", ".join(step.get("target_materials", []))), "启用" if step.get("enabled", True) else "停用"))
        count = len(self.data["process"]["steps"])
        if select is None and count:
            select = min(previous if previous is not None else 0, count - 1)
        if select is not None and 0 <= select < count:
            self.step_tree.selection_set(str(select))
            self.step_tree.focus(str(select))
            self.step_tree.see(str(select))
            self.select_step()
        else:
            self.clear_step_form()

    def clear_step_form(self):
        self._filling_step = True
        self.step_index = None
        self.step_dirty = False
        for variable in self.step_vars.values():
            variable.set("")
        self.step_enabled.set(False)
        self._filling_step = False
        self.step_hint.set("暂无选中步骤；新增薄膜或在上方选择已有步骤。")
        self._update_step_controls()

    def select_step(self, event=None):
        selected = self.step_tree.selection()
        if not selected:
            return
        index = int(selected[0])
        if self.step_index == index:
            return
        if self.step_dirty:
            previous = self.step_index
            try:
                self.apply_step(refresh=False)
            except Exception as exc:
                self.step_tree.selection_set(str(previous))
                self.status.set("先修正当前步骤：" + str(exc))
                return
        self.step_index = index
        step = self.data["process"]["steps"][index]
        self._filling_step = True
        values = {"name": step.get("name", step["id"]),
                  "operation": next((k for k, v in OPS.items() if v == step["operation"]), "按掩模添加"),
                  "material": step.get("material", ", ".join(step.get("target_materials", []))),
                  "film_mode": next((k for k, v in FILMS.items() if v == step.get("film_mode", "absolute")), "指定绝对高度（高级）"),
                  "z": 1000 * step.get("start_z_um" if step["operation"] == "etch" else "z_min_um", 0),
                  "thickness": 1000 * step.get("thickness_um", step.get("z_max_um", 0) - step.get("z_min_um", 0)),
                  "sidewall": 1000 * step.get("sidewall_thickness_um", .1), "etch_depth": 1000 * step.get("etch_depth_um", .1),
                  "cd_bias": 1000 * step.get("cd_bias_um", 0), "dx": 1000 * step.get("delta_x_um", 0),
                  "dy": 1000 * step.get("delta_y_um", 0), "rotation": step.get("rotation_deg", 0)}
        for key, value in values.items():
            self.step_vars[key].set(str(value))
        self.step_enabled.set(step.get("enabled", True))
        self._filling_step = False
        self.step_dirty = False
        mask = step.get("mask") or {}
        mask_note = (f"GDS: {mask.get('cell')} / layer {mask.get('layer')} / datatype {mask.get('datatype')}" if "gds_path" in mask
                     else "自定义多边形掩模" if mask else "无掩模：作用于整个计算单元")
        self.step_hint.set(f"步骤 ID: {step['id']}  |  {mask_note}。长度输入为 nm；z 是绝对坐标。")
        self._update_step_controls()

    def apply_step(self, refresh=True):
        if self.step_index is None:
            raise ValueError("请先选中工艺步骤")
        step = copy.deepcopy(self.data["process"]["steps"][self.step_index])
        op = OPS[self.step_vars["operation"].get()]
        def number(name, unit=.001):
            labels = {"z": "z 起点 / 参考面", "thickness": "厚度 / 删除范围", "sidewall": "侧壁厚度",
                      "etch_depth": "刻蚀深度", "cd_bias": "CD 总偏置", "dx": "Δx", "dy": "Δy", "rotation": "旋转角"}
            try:
                value = float(self.step_vars[name].get()) * unit
            except ValueError:
                raise ValueError(labels[name] + " 请填写数字") from None
            if not math.isfinite(value):
                raise ValueError(labels[name] + " 必须为有限数")
            return value
        step.update(name=self.step_vars["name"].get().strip() or step["id"], operation=op,
                    enabled=self.step_enabled.get())
        step.setdefault("sidewall_angle_deg", 90)
        # Remove parameters owned by a previous operation. In particular a
        # negative old mask would otherwise turn a new full-cell layer empty.
        retained = {"create_substrate": {"z_min_um", "thickness_um"},
                    "add": {"z_min_um", "thickness_um"}, "delete": {"z_min_um", "z_max_um"},
                    "etch": {"start_z_um", "etch_depth_um"},
                    "create_film": {"film_mode", "z_min_um", "thickness_um", "sidewall_thickness_um"}}[op]
        for key in {"z_min_um", "z_max_um", "thickness_um", "start_z_um", "etch_depth_um", "film_mode", "sidewall_thickness_um"} - retained:
            step.pop(key, None)
        names = list(dict.fromkeys(v.strip() for v in self.step_vars["material"].get().replace("，", ",").split(",") if v.strip()))
        if not names or any(name not in self.data["materials"]["materials"] for name in names):
            raise ValueError("请使用工程材料库中的材料名称")
        if op in ("etch", "delete"):
            step.pop("material", None)
            step["target_materials"] = names
        else:
            if len(names) != 1:
                raise ValueError("添加步骤只允许一个材料")
            step["material"] = names[0]
            step.pop("target_materials", None)
        if op in ("create_substrate", "add", "delete"):
            step["z_min_um"] = number("z")
            step.pop("z_max_um", None)
            step["thickness_um"] = number("thickness")
            if step["thickness_um"] <= 0:
                raise ValueError("厚度必须大于零")
            if op == "delete":
                step["z_max_um"] = step["z_min_um"] + step.pop("thickness_um")
        if op == "etch":
            step.update(start_z_um=number("z"), etch_depth_um=number("etch_depth"))
            if step["etch_depth_um"] <= 0:
                raise ValueError("刻蚀深度必须大于零")
        if op == "create_film":
            mode = FILMS[self.step_vars["film_mode"].get()]
            step.update(film_mode=mode, thickness_um=number("thickness"))
            step.pop("z_max_um", None)
            if step["thickness_um"] <= 0:
                raise ValueError("薄膜顶部厚度必须大于零")
            if mode == "absolute":
                step["z_min_um"] = number("z")
            else:
                step.pop("z_min_um", None)
            if mode == "conformal":
                step["sidewall_thickness_um"] = number("sidewall")
                if step["sidewall_thickness_um"] < 0:
                    raise ValueError("侧壁厚度不能为负数")
            else:
                step.pop("sidewall_thickness_um", None)
        if op in ("add", "etch"):
            step.update(cd_bias_um=number("cd_bias"), delta_x_um=number("dx"), delta_y_um=number("dy"), rotation_deg=number("rotation", 1))
        else:
            for key in ("mask", "mask_polarity", "cd_bias_um", "delta_x_um", "delta_y_um", "rotation_deg"):
                step.pop(key, None)
        self.data["process"]["steps"][self.step_index] = step
        if (self.data["process"].get("mark", {}).get("target_step_id") == step["id"]
                and (not step["enabled"] or op not in ("add", "etch"))):
            # A disabled/repurposed mask is no longer an active AH template.
            # Keep the actual GDS and step, drop only the invalid declaration.
            self.data["process"].pop("mark", None)
        self.step_dirty = False
        self.changed()
        if refresh:
            self.refresh_steps(select=self.step_index)
        self.refresh_mark()
        self.status.set("步骤已应用到工程草稿；保存输入或生成结构。")

    def add_step(self):
        self.require_project()
        if self.step_dirty:
            self.apply_step()
        names = list(self.data["materials"]["materials"])
        background = self.data["process"].get("background_material")
        material = next((n for n in reversed(names) if n != background), names[0])
        self.data["process"]["steps"].append({"id": "film_" + uuid4().hex[:10], "name": "新表面薄膜", "operation": "create_film", "material": material,
                                               "film_mode": "conformal", "thickness_um": .1, "sidewall_thickness_um": .1, "enabled": True, "sidewall_angle_deg": 90})
        self.refresh_steps(select=len(self.data["process"]["steps"]) - 1)
        self.changed()

    def delete_step(self):
        if self.step_index is None:
            raise ValueError("请先选中步骤")
        steps = self.data["process"]["steps"]
        deleted = steps.pop(self.step_index)
        if self.data["process"].get("mark", {}).get("target_step_id") == deleted["id"]:
            self.data["process"].pop("mark", None)
        if self.mark_vars["target_step_id"].get() == deleted["id"]:
            self.mark_dirty = False
        self.refresh_steps()
        self.refresh_mark()
        self.changed()

    def move_step(self, direction):
        if self.step_index is None:
            raise ValueError("请先选中步骤")
        if self.step_dirty:
            self.apply_step()
        i = self.step_index
        j = i + direction
        steps = self.data["process"]["steps"]
        if 0 <= j < len(steps):
            steps[i], steps[j] = steps[j], steps[i]
            self.refresh_steps(select=j)
            self.changed()

    def edit_recipe(self):
        self.require_project()
        self.commit_pending_inputs()
        def apply(value):
            if not isinstance(value, dict) or not isinstance(value.get("steps"), list):
                raise ValueError("工艺必须包含 steps 数组")
            self._validate_steps(value["steps"])
            self.data["process"] = value
            self.refresh_steps()
            self.refresh_mark()
            self.changed()
        JsonDialog(self, "完整工艺 · µm 单位 / 计算单元 / 背景 / 自定义 GDS", self.data["process"], apply)

    def edit_step_json(self):
        if self.step_index is None:
            raise ValueError("请先选中步骤")
        self.commit_pending_inputs()
        index = self.step_index
        def apply(value):
            if not isinstance(value, dict) or not value.get("id") or value.get("operation") not in OPS.values():
                raise ValueError("步骤需要 id 和有效 operation")
            steps = copy.deepcopy(self.data["process"]["steps"])
            steps[index] = value
            self._validate_steps(steps)
            original = self.data["process"]["steps"][index]
            old_id = original["id"]
            self.data["process"]["steps"][index] = value
            if (self.data["process"].get("mark", {}).get("target_step_id") == old_id
                    and (value["id"] != old_id or value["operation"] not in ("add", "etch")
                         or not value.get("enabled", True) or value.get("mask") != original.get("mask"))):
                self.data["process"].pop("mark", None)
            self.refresh_steps(select=index)
            self.refresh_mark(force=True)
            self.changed()
        JsonDialog(self, "高级步骤 · ROI / 正反掩模 / 几何参数", self.data["process"]["steps"][index], apply)

    @staticmethod
    def _validate_steps(steps):
        ids = []
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("id"), str) or not step["id"].strip():
                raise ValueError("每个步骤需要非空文本 ID")
            if step.get("operation") not in OPS.values():
                raise ValueError(f"步骤 {step['id']} 的 operation 无效")
            if step.get("film_mode", "absolute") not in FILMS.values():
                raise ValueError(f"步骤 {step['id']} 的 film_mode 无效")
            if type(step.get("enabled", True)) is not bool:
                raise ValueError(f"步骤 {step['id']} 的 enabled 必须为 true / false")
            if "target_materials" in step and (not isinstance(step["target_materials"], list)
                                               or any(not isinstance(name, str) for name in step["target_materials"])):
                raise ValueError(f"步骤 {step['id']} 的 target_materials 必须为材料名称数组")
            if step.get("mask") is not None and not isinstance(step["mask"], dict):
                raise ValueError(f"步骤 {step['id']} 的 mask 必须为对象或 null")
            ids.append(step["id"])
        if len(ids) != len(set(ids)):
            raise ValueError("步骤 ID 不能重复")

    def bind_gds(self, path, cell, layer, datatype):
        if self.step_index is None:
            raise ValueError("先选择需要掩模的工艺步骤")
        if self.step_dirty:
            self.apply_step()
        step = self.data["process"]["steps"][self.step_index]
        if step["operation"] not in ("add", "etch"):
            raise ValueError("GDS 掩模只用于添加 / 刻蚀步骤")
        if self.mark_dirty and self.mark_vars["target_step_id"].get() == step["id"]:
            raise ValueError("此步骤还有待应用 Mark 尺寸，请先应用；再导入自定义 GDS，避免模板覆盖自定义掩模")
        if isinstance(layer, bool) or isinstance(datatype, bool) or str(layer) != str(int(layer)) or str(datatype) != str(int(datatype)):
            raise ValueError("Layer / Datatype 必须是整数")
        actual = inspect_gds_choices(path)
        if cell not in actual["cells"] or (int(layer), int(datatype)) not in actual["cells"][cell]:
            raise ValueError("cell 或 layer/datatype 在实际 GDS 中不存在")
        destination = Path(self.data["folder"]) / "inputs" / ("import_" + uuid4().hex[:10] + ".gds")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        step["mask"] = {"gds_path": destination.relative_to(self.data["folder"]).as_posix(), "cell": cell, "layer": int(layer), "datatype": int(datatype)}
        if self.data["process"].get("mark", {}).get("target_step_id") == step["id"]:
            self.data["process"].pop("mark", None)
        self.refresh_steps(select=self.step_index)
        self.refresh_mark()
        self.changed()

    def gds_dialog(self):
        self.require_project()
        if self.step_index is None:
            raise ValueError("请先选中添加或刻蚀步骤")
        if OPS.get(self.step_vars["operation"].get()) not in ("add", "etch"):
            raise ValueError("GDS 掩模只用于添加 / 刻蚀步骤")
        path = filedialog.askopenfilename(title="选择原始 GDS（复制至工程，保留源文件）", filetypes=[("GDS", "*.gds *.gdsii")], parent=self.winfo_toplevel())
        if not path:
            return
        info = inspect_gds_choices(path)
        if not any(info["cells"].values()):
            raise ValueError("该 GDS 没有可用多边形；请选择含有实际图形的文件")
        window = tk.Toplevel(self)
        window.title("选择 GDS 实际 cell 与图层")
        window.geometry("650x330")
        make_modal(window, self)
        box = ttk.Frame(window, padding=18)
        box.pack(fill="both", expand=True)
        hint(box, f"单位 {info['unit_m']:g} m；精度 {info['precision_m']:g} m。包含 cell 引用后的实际 polygon 图层。")
        form = ttk.Frame(box)
        form.pack(fill="x")
        cell = tk.StringVar()
        spec = tk.StringVar()
        cells = sorted(name for name, specs in info["cells"].items() if specs)
        cell_combo = field(form, 0, 0, "Cell", cell, values=cells)
        specs_combo = field(form, 1, 0, "Layer / Datatype", spec, values=[])
        def changed(*_):
            values = [f"{l} / {d}" for l, d in info["cells"].get(cell.get(), [])]
            specs_combo.configure(values=values)
            spec.set(values[0] if values else "")
        cell.trace_add("write", changed)
        cell.set(cells[0] if cells else "")
        def apply():
            if not spec.get():
                raise ValueError("请选择包含多边形的 Cell 和 Layer / Datatype")
            layer, datatype = [int(v.strip()) for v in spec.get().split("/")]
            self.bind_gds(path, cell.get(), layer, datatype)
            window.destroy()
        self.button(box, "使用选中图层", apply, primary=True)
