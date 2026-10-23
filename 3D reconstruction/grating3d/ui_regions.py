"""Material-region form editor with explicit coverage and one-based ridge labels."""
from __future__ import annotations

import copy
import json
import math
import re
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk


KINDS = {"substrate": "基底", "ridge": "光栅条", "ridge_layer": "光栅分层", "layer": "水平材料层", "box": "局部长方体"}


def parse_ridge_numbers(text, ridge_count=None):
    """Convert visible ridge numbers such as ``1,3-5`` to zero-based indices."""
    if not text.strip():
        return None
    values = []
    for token in re.split(r"[,，;；\s]+", text.strip()):
        if not token:
            continue
        if "-" in token:
            endpoints = token.split("-")
            if len(endpoints) != 2:
                raise ValueError("光栅编号可写 1,3-5；编号从 1 开始。")
            start, end = map(int, endpoints)
            if end < start:
                raise ValueError("光栅编号区间必须递增，例如 3-5。")
            values.extend(range(start, end + 1))
        else:
            values.append(int(token))
    if not values or min(values) < 1:
        raise ValueError("光栅编号从 1 开始，留空表示所有光栅。")
    if ridge_count is not None and max(values) > ridge_count:
        raise ValueError(f"当前共有 {ridge_count} 条光栅，编号不能超出范围。")
    return sorted(set(value - 1 for value in values))


class RegionEditor(ttk.Frame):
    def __init__(self, parent, materials=None, on_change=None):
        super().__init__(parent)
        self.on_change = on_change
        self.materials = list(materials or [])
        self._regions = []
        self._coverage = {}
        self.domain = {"x_bounds_nm": [0, 2400], "y_bounds_nm": [0, 1800],
                       "height_nm": 216, "substrate_depth_nm": 200, "ridge_count": None}
        self.advanced = tk.BooleanVar(value=False)
        self.summary = tk.StringVar(value="区域从上到下赋值，后面的区域覆盖前面的重叠部分。")
        self._build()

    def _build(self):
        self.description_label = ttk.Label(self, text="在表单里指定材料的位置；z = 0 为基底上表面，正 z 向上。光栅编号从 1 开始。", wraplength=820)
        self.description_label.pack(anchor="w", pady=(0, 8))
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=(0, 7))
        for label, callback in [("＋ 添加区域", self.add_region), ("编辑", self.edit_region),
                                ("删除", self.delete_region), ("↑ 先赋值", lambda: self.move_region(-1)),
                                ("↓ 后覆盖", lambda: self.move_region(1))]:
            ttk.Button(buttons, text=label, command=callback).pack(side="left", padx=(0, 7))
        ttk.Checkbutton(buttons, text="高级 JSON", variable=self.advanced, command=self._toggle_advanced).pack(side="right")
        columns = ("order", "name", "kind", "material", "range", "ridge", "coverage")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=8, selectmode="browse")
        titles = ["覆盖顺序", "区域名称", "区域类型", "材料", "z 范围 (nm)", "光栅编号", "最终体素数"]
        widths = [75, 170, 110, 120, 150, 115, 135]
        for col, title, width in zip(columns, titles, widths):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, minwidth=60, stretch=True)
        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True)
        self.tree.pack(in_=tree_frame, side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.tag_configure("empty", background="#fff0ec", foreground="#a02e23")
        self.tree.bind("<Double-1>", lambda _: self.edit_region())
        self.summary_label = ttk.Label(self, textvariable=self.summary, wraplength=820)
        self.summary_label.pack(anchor="w", pady=(7, 3))
        self.coverage_label = ttk.Label(self, text="最终体素数在重建后更新。0 表示区域未进入网格或被其他区域完全覆盖。", wraplength=820)
        self.coverage_label.pack(anchor="w")
        self.json_frame = ttk.LabelFrame(self, text="高级：直接编辑配置数组", padding=8)
        self.json_editor = scrolledtext.ScrolledText(self.json_frame, height=8, font=("Consolas", 10), wrap="none")
        self.json_editor.pack(fill="both", expand=True)
        ttk.Button(self.json_frame, text="应用 JSON 到表单", command=self.apply_json).pack(anchor="e", pady=(7, 0))
        self.bind("<Configure>", self._resize_labels)

    def _resize_labels(self, event):
        if event.widget == self:
            for label in (self.description_label, self.summary_label, self.coverage_label):
                label.configure(wraplength=max(200, event.width - 12))

    def set_materials(self, materials):
        self.materials = list(materials)

    def set_domain(self, geometry):
        if not geometry:
            return
        for key in ("x_bounds_nm", "y_bounds_nm", "height_nm", "ridge_count", "substrate_depth_nm"):
            if key in geometry:
                self.domain[key] = copy.deepcopy(geometry[key])
        if "ridge_heights_nm" in geometry:
            self.domain["height_nm"] = max(float(value) for value in geometry["ridge_heights_nm"])
        bounds = geometry.get("bounds_nm")
        if bounds and len(bounds) == 3:
            self.domain["x_bounds_nm"], self.domain["y_bounds_nm"] = list(bounds[0]), list(bounds[1])
            if bounds[2][0] < 0:
                self.domain["substrate_depth_nm"] = -bounds[2][0]
        self._refresh()

    def set_regions(self, regions):
        if not isinstance(regions, list):
            raise ValueError("材料区域必须是数组。")
        self._regions = copy.deepcopy(regions)
        self._coverage = {}
        self._refresh()

    def get_regions(self):
        if self.advanced.get():
            text = self.json_editor.get("1.0", "end").strip()
            try:
                edited = json.loads(text)
            except ValueError:
                raise ValueError("高级 JSON 尚未正确填写，请先点击‘应用 JSON 到表单’。") from None
            if edited != self._regions:
                raise ValueError("高级 JSON 已修改，请先点击‘应用 JSON 到表单’。")
        return copy.deepcopy(self._regions)

    def set_coverage(self, regions):
        if isinstance(regions, dict):
            regions = regions.get("regions", [])
        self._coverage = {}
        for index, item in enumerate(regions or []):
            key = int(item.get("region_id", index + 1)) - 1
            self._coverage[key] = item
        self._refresh()

    def _refresh(self, selected=None):
        self.tree.delete(*self.tree.get_children())
        priorities = sorted(range(len(self._regions)), key=lambda index: float(self._regions[index].get("priority", 0)))
        for order, index in enumerate(priorities, 1):
            region = self._regions[index]
            kind = region.get("kind", "?")
            if kind == "substrate":
                extent = f"−{self.domain['substrate_depth_nm']:g} 至 0"
            elif kind == "ridge":
                extent = f"0 至 {self.domain['height_nm']:g}"
            elif kind == "ridge_layer":
                extent = f"{region.get('z_min_nm', '?')} 至 {region.get('z_max_nm', '?')}"
            else:
                extent = " 至 ".join(map(str, region.get("z_range_nm", ["?", "?"])))
            indices = region.get("ridge_indices")
            numbers = ", ".join(str(value + 1) for value in indices) if indices is not None else "所有"
            if kind not in {"ridge", "ridge_layer"}:
                numbers = "—"
            coverage = self._coverage.get(index)
            final = coverage.get("final_voxels") if coverage else None
            count = "待重建" if final is None else ("0 · 检查区域" if final == 0 else f"{final:,}")
            self.tree.insert("", "end", iid=str(index), values=(order, region.get("name", f"区域 {index + 1}"),
                             KINDS.get(kind, kind), region.get("material", "?"), extent, numbers, count),
                             tags=("empty",) if final == 0 else ())
        if selected is not None and self.tree.exists(str(selected)):
            self.tree.selection_set(str(selected))
            self.tree.see(str(selected))
        self._sync_json()
        empty = sum(item.get("final_voxels") == 0 for item in self._coverage.values())
        self.summary.set(f"共 {len(self._regions)} 个材料区域。" + (f" {empty} 个区域最终体素数为 0，请检查范围和覆盖顺序。" if empty else " 顺序越靠后，覆盖越晚。"))

    def _selected(self):
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def add_region(self, kind="ridge"):
        if not self.materials:
            messagebox.showinfo("材料库", "请先载入材料库。", parent=self)
            return
        material = "silica" if "silica" in self.materials else self.materials[0]
        number = len(self._regions) + 1
        defaults = {"name": f"区域 {number}", "kind": kind, "material": material,
                    "priority": max([float(r.get("priority", 0)) for r in self._regions] + [-1]) + 1}
        dialog = RegionDialog(self, defaults, self.materials, self.domain)
        if dialog.result is not None:
            self._regions.append(dialog.result)
            self._changed(len(self._regions) - 1)

    def edit_region(self):
        index = self._selected()
        if index is None:
            self.summary.set("请先点击需要编辑的材料区域。")
            return
        dialog = RegionDialog(self, self._regions[index], self.materials, self.domain)
        if dialog.result is not None:
            self._regions[index] = dialog.result
            self._changed(index)

    def delete_region(self):
        index = self._selected()
        if index is not None:
            del self._regions[index]
            self._changed(min(index, len(self._regions) - 1))

    def move_region(self, direction):
        index = self._selected()
        if index is None:
            return
        priorities = sorted(range(len(self._regions)), key=lambda value: float(self._regions[value].get("priority", 0)))
        position = priorities.index(index)
        target = position + direction
        if not 0 <= target < len(priorities):
            return
        priorities[position], priorities[target] = priorities[target], priorities[position]
        for order, original_index in enumerate(priorities):
            self._regions[original_index]["priority"] = order
        self._changed(index)

    def _changed(self, selected=None):
        self._coverage = {}
        self._refresh(selected)
        if self.on_change:
            self.on_change()

    def _toggle_advanced(self):
        if self.advanced.get():
            self._sync_json()
            self.json_frame.pack(fill="both", pady=(10, 0))
        else:
            self.json_frame.pack_forget()

    def _sync_json(self):
        self.json_editor.delete("1.0", "end")
        self.json_editor.insert("1.0", json.dumps(self._regions, ensure_ascii=False, indent=2))

    def apply_json(self):
        try:
            regions = json.loads(self.json_editor.get("1.0", "end"))
            if not isinstance(regions, list) or any(not isinstance(item, dict) for item in regions):
                raise ValueError("请输入由区域对象组成的数组。")
            for region in regions:
                if region.get("kind") not in KINDS:
                    raise ValueError(f"未知区域类型：{region.get('kind')}")
                if region.get("material") not in self.materials:
                    raise ValueError(f"材料不在当前库中：{region.get('material')}")
                if not math.isfinite(float(region.get("priority", 0))):
                    raise ValueError("覆盖优先级必须是有限数值。")
        except (ValueError, TypeError) as error:
            messagebox.showerror("JSON 配置", str(error), parent=self)
            return
        self._regions = regions
        self._changed()


class RegionDialog(tk.Toplevel):
    """Small type-aware form. ``wait=False`` allows deterministic UI smoke checks."""
    def __init__(self, parent, region, materials, domain, wait=True):
        super().__init__(parent)
        self.result = None
        self.region = copy.deepcopy(region)
        self.materials = list(materials)
        self.domain = domain
        self.title("材料区域")
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self.fields = {}
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        self.frame = frame
        name = region.get("name", "材料区域")
        self._entry("name", "区域名称", name, 0)
        self.kind_var = tk.StringVar(value=KINDS.get(region.get("kind", "ridge"), "光栅条"))
        ttk.Label(frame, text="区域类型").grid(row=1, column=0, sticky="w", pady=5)
        combo = ttk.Combobox(frame, textvariable=self.kind_var, values=list(KINDS.values()), state="readonly", width=28)
        combo.grid(row=1, column=1, sticky="ew", pady=5)
        combo.bind("<<ComboboxSelected>>", lambda _: self._kind_changed())
        self.material_var = tk.StringVar(value=region.get("material", materials[0] if materials else ""))
        ttk.Label(frame, text="材料").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Combobox(frame, textvariable=self.material_var, values=self.materials, state="readonly", width=28).grid(row=2, column=1, sticky="ew", pady=5)
        self._entry("priority", "覆盖优先级", region.get("priority", 0), 3)
        indices = region.get("ridge_indices", [])
        self._entry("ridge_indices", "光栅编号（从 1 开始）", ",".join(str(i + 1) for i in indices), 4)
        height = float(domain.get("height_nm", 216))
        xr, yr = domain.get("x_bounds_nm", [0, 2400]), domain.get("y_bounds_nm", [0, 1800])
        zr = region.get("z_range_nm", [region.get("z_min_nm", 0), region.get("z_max_nm", height)])
        self._entry("z_min", "z 起点 (nm)", zr[0], 5)
        self._entry("z_max", "z 终点 (nm)", zr[1], 6)
        for key, label, values, row in [("x", "x", region.get("x_range_nm", xr), 7), ("y", "y", region.get("y_range_nm", yr), 9)]:
            self._entry(f"{key}_min", f"{label} 起点 (nm)", values[0], row)
            self._entry(f"{key}_max", f"{label} 终点 (nm)", values[1], row + 1)
        self.help_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.help_var, wraplength=450).grid(row=11, column=0, columnspan=2, sticky="w", pady=(10, 8))
        buttons = ttk.Frame(frame)
        buttons.grid(row=12, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="left", padx=8)
        self.save_button = ttk.Button(buttons, text="保存区域", command=self.accept)
        self.save_button.pack(side="left")
        self._kind_changed()
        self.bind("<Return>", lambda _: self.accept())
        self.bind("<Escape>", lambda _: self.destroy())
        if wait:
            self.grab_set()
            self.wait_window()

    def _entry(self, key, label, value, row):
        variable = tk.StringVar(value=str(value))
        text = ttk.Label(self.frame, text=label)
        entry = ttk.Entry(self.frame, textvariable=variable, width=30)
        text.grid(row=row, column=0, sticky="w", padx=(0, 14), pady=5)
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        self.fields[key] = (variable, text, entry)

    def _kind(self):
        return next(key for key, title in KINDS.items() if title == self.kind_var.get())

    def _kind_changed(self):
        kind = self._kind()
        visible = {"name", "priority"}
        if kind in {"ridge", "ridge_layer"}:
            visible.add("ridge_indices")
        if kind in {"ridge_layer", "box", "layer"}:
            visible |= {"z_min", "z_max"}
        if kind == "box":
            visible |= {"x_min", "x_max", "y_min", "y_max"}
        for key, (_, label, entry) in self.fields.items():
            if key in visible:
                label.grid()
                entry.grid()
            else:
                label.grid_remove()
                entry.grid_remove()
        help_text = {"substrate": "覆盖 z < 0 的整个基底。厚度在网格设置中指定。",
                     "ridge": "覆盖全部光栅高度。编号留空表示所有光栅，可填 1,3-5。",
                     "ridge_layer": "仅覆盖所选光栅内的 z 区间；可用于不同材料叠层。",
                     "layer": "覆盖整个横向范围的 z 区间，也会覆盖空气和光栅。",
                     "box": "覆盖指定长方体。选择 air 可定义局部空洞。"}
        self.help_var.set(help_text[kind] + " 数值越大，覆盖越晚。")

    def _number(self, key):
        value = float(self.fields[key][0].get())
        if not math.isfinite(value):
            raise ValueError("位置和覆盖优先级必须是有限数值。")
        return value

    def accept(self):
        try:
            kind = self._kind()
            name = self.fields["name"][0].get().strip()
            if not name:
                raise ValueError("请填写区域名称。")
            material = self.material_var.get()
            if material not in self.materials:
                raise ValueError("请选择当前材料库中的材料。")
            result = {"name": name, "kind": kind, "material": material, "priority": self._number("priority")}
            if kind in {"ridge", "ridge_layer"}:
                selected = parse_ridge_numbers(self.fields["ridge_indices"][0].get(), self.domain.get("ridge_count"))
                if selected is not None:
                    result["ridge_indices"] = selected
            if kind in {"ridge_layer", "box", "layer"}:
                lo, hi = self._number("z_min"), self._number("z_max")
                if hi <= lo:
                    raise ValueError("z 终点必须大于起点。")
                if kind == "ridge_layer":
                    result.update(z_min_nm=lo, z_max_nm=hi)
                else:
                    result["z_range_nm"] = [lo, hi]
            if kind == "box":
                for axis in "xy":
                    lo, hi = self._number(f"{axis}_min"), self._number(f"{axis}_max")
                    if hi <= lo:
                        raise ValueError(f"{axis} 终点必须大于起点。")
                    result[f"{axis}_range_nm"] = [lo, hi]
        except (ValueError, TypeError) as error:
            messagebox.showerror("材料区域", str(error), parent=self)
            return
        self.result = result
        self.destroy()
