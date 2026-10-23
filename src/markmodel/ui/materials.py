"""Local material tables with explicit wavelength units and missing-k policy."""
from __future__ import annotations

import copy
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from .widgets import JsonDialog, card, field, hint, make_modal
from .material_import import read_material_table, material_spec_from_table, columns_from_fields


class MaterialsPage:
    def create_materials_page(self, page):
        box = card(page, "工程材料库 · 与结构一起传递")
        hint(box, "默认材料名含 DEMO，仅用于演示。请导入自己的 n、k 数据。介电常数 εr = (n + ik)²；建模允许金属，具体求解器的限制在导入时检查。")
        table_box = ttk.Frame(box)
        table_box.pack(fill="x", pady=8)
        self.material_tree = ttk.Treeview(table_box, columns=("range", "k", "usage", "source"), show="tree headings", height=6, selectmode="browse")
        for key, title, width in (("#0", "材料", 190), ("range", "波长范围 nm", 165), ("k", "吸收数据", 200), ("usage", "工艺引用", 180), ("source", "来源", 220)):
            self.material_tree.heading(key, text=title)
            self.material_tree.column(key, width=width, minwidth=90)
        vertical = ttk.Scrollbar(table_box, orient="vertical", command=self.material_tree.yview)
        horizontal = ttk.Scrollbar(table_box, orient="horizontal", command=self.material_tree.xview)
        self.material_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.material_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        table_box.columnconfigure(0, weight=1)
        self.material_tree.bind("<<TreeviewSelect>>", self.select_material)
        # Recompute references when returning from the process page.
        self.material_tree.bind("<Map>", lambda _event: self.refresh_materials())
        bar = ttk.Frame(box)
        bar.pack(fill="x")
        self.button(bar, "导入 TXT / CSV…", self.material_import_dialog, primary=True)
        self.button(bar, "新增常数 n / k 材料…", self.constant_material_dialog)
        self.button(bar, "高级：材料表 / 来源", self.edit_material)
        self.button(bar, "删除未使用材料", self.delete_material)
        self.material_lossless = tk.BooleanVar(value=False)
        self.lossless_check = ttk.Checkbutton(box, text="所选材料缺少 k 时，由我明确采用 k=0 无损近似（写入导出记录）", variable=self.material_lossless, command=lambda: self.safe(self.set_lossless))
        self.lossless_check.pack(anchor="w", pady=8)
        self.material_note = tk.StringVar(value="选择材料查看 n / k 光谱；缺少 k 不会自动置零。")
        ttk.Label(box, textvariable=self.material_note, style="Hint.TLabel", wraplength=1050).pack(fill="x")
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        self.nk_figure = Figure(figsize=(10, 3), dpi=100, facecolor="#f2f5fa")
        self.nk_axes = self.nk_figure.subplots(1, 2)
        self.nk_canvas = FigureCanvasTkAgg(self.nk_figure, master=box)
        self.nk_canvas.get_tk_widget().pack(fill="x", pady=8)
        self._clear_material_selection()
        info = card(page, "操作顺序")
        hint(info, "① 建立材料表 → ② 设置 Mark、GDS 与工艺 → ③ 生成并核对结构 → ④ 根据需要接入 SEM/TEM 或体素 → ⑤ 导出自家结构包与 Lumerical 形状。\n输入编辑与几何快照分开记录。输入改变后必须重新生成，避免导出旧结构。")

    def _clear_material_selection(self):
        self.material_lossless.set(False)
        self.lossless_check.configure(state="disabled")
        self.material_note.set("尚未选择材料。新建或打开工程后，可查看材料光谱与使用位置。")
        for axis, label in zip(self.nk_axes, ("n", "k")):
            axis.clear()
            axis.set_xlabel("Vacuum wavelength (nm)")
            axis.set_ylabel(label)
            axis.grid(alpha=.18)
            axis.text(.5, .5, "Select a material", transform=axis.transAxes, ha="center", color="#52677e")
        self.nk_figure.tight_layout()
        self.nk_canvas.draw_idle()

    def material_references(self, name):
        """Include disabled steps and unapplied forms: they must not break later."""
        if not self.data:
            return []
        process = self.data["process"]
        references = []
        for key, label in (("background_material", "计算域背景"), ("substrate_material", "衬底定义")):
            if process.get(key) == name:
                references.append(label)
        for index, step in enumerate(process.get("steps", []), 1):
            if step.get("material") == name or name in step.get("target_materials", []):
                references.append(f"步骤 {index:02d} {step.get('name', step.get('id', ''))}" + ("（停用）" if not step.get("enabled", True) else ""))
        if getattr(self, "step_dirty", False) and name in [v.strip() for v in self.step_vars["material"].get().split(",")]:
            references.append("尚未应用的步骤表单")
        return references

    def refresh_materials(self, select=None):
        previous = self.material_tree.selection()
        self.material_tree.delete(*self.material_tree.get_children())
        if not self.data:
            self._clear_material_selection()
            if hasattr(self, "step_controls"):
                self.step_controls["material"].configure(values=[])
            return
        materials = self.data["materials"]["materials"]
        for name, spec in materials.items():
            wave = spec["wavelength_nm"]
            k = "提供 k" if spec.get("k") is not None else ("未知；明确假设 k=0" if spec.get("assume_lossless") else "未知；光学导出前需决定")
            refs = self.material_references(name)
            source = spec.get("source", spec.get("original_source", "未记录"))
            self.material_tree.insert("", "end", iid=name, text=name, values=(f"{min(wave):g} – {max(wave):g}", k,
                                            f"{len(refs)} 处引用" if refs else "未用于工艺", Path(str(source)).name))
        if hasattr(self, "step_controls"):
            self.step_controls["material"].configure(values=list(materials))
        name = select or (previous[0] if previous else None)
        name = name if name in materials else next(iter(materials), None)
        if name:
            self.material_tree.selection_set(name)
            self.material_tree.see(name)
            self.select_material()
        else:
            self._clear_material_selection()

    def select_material(self, event=None):
        selected = self.material_tree.selection()
        if not selected or not self.data or selected[0] not in self.data["materials"]["materials"]:
            self._clear_material_selection()
            return
        name = selected[0]
        spec = self.data["materials"]["materials"][name]
        self.material_lossless.set(spec.get("k") is None and spec.get("assume_lossless", False))
        self.lossless_check.configure(state="disabled" if spec.get("k") is not None else "normal")
        for axis in self.nk_axes:
            axis.clear()
            axis.grid(alpha=.18)
            axis.set_xlabel("Vacuum wavelength (nm)")
        self.nk_axes[0].plot(spec["wavelength_nm"], spec["n"], color="#2667c9", marker=".")
        self.nk_axes[0].set_ylabel("n")
        if spec.get("k") is not None:
            self.nk_axes[1].plot(spec["wavelength_nm"], spec["k"], color="#d38331", marker=".")
        elif spec.get("assume_lossless"):
            self.nk_axes[1].plot(spec["wavelength_nm"], [0] * len(spec["n"]), linestyle="--", color="#d38331", label="Explicit k=0 assumption")
            self.nk_axes[1].legend()
        else:
            self.nk_axes[1].text(.5, .5, "k UNKNOWN\nNo automatic lossless fallback", transform=self.nk_axes[1].transAxes, ha="center")
        self.nk_axes[1].set_ylabel("k")
        self.nk_figure.tight_layout()
        self.nk_canvas.draw_idle()
        refs = "；".join(self.material_references(name)) or "未用于当前工艺（新增材料不会自动替换结构中的材料）"
        k_note = "k 来自材料表" if spec.get("k") is not None else ("k 未知，已明确采用 k=0 近似" if spec.get("assume_lossless") else "k 未知；可保存建模，但光学导出前需提供 k 或明确采用无损近似")
        self.material_note.set(f"{name} · {k_note}\n使用位置：{refs}\n来源：{spec.get('source', spec.get('original_source', '未记录'))} · 仅在表内插值，不外推")

    def set_lossless(self):
        selected = self.material_tree.selection()
        if not selected:
            raise ValueError("先选中材料")
        name = selected[0]
        spec = self.data["materials"]["materials"][name]
        if spec.get("k") is not None:
            self.material_lossless.set(False)
            raise ValueError("此材料已有 k，不能用缺失数据近似覆盖")
        if spec.get("assume_lossless", False) == bool(self.material_lossless.get()):
            return
        spec["assume_lossless"] = bool(self.material_lossless.get())
        self.refresh_materials(select=name)
        self.changed()
        self.status.set(f"{name}：" + ("明确采用 k=0 无损近似" if spec["assume_lossless"] else "恢复为 k 未知") + "；保存并重新生成后生效。")

    def _check_material_name(self, name):
        self.require_project()
        name = name.strip()
        if not name:
            raise ValueError("材料名称不能为空")
        if any(char in name for char in ",，;；\n\r\t"):
            raise ValueError("材料名称不能含逗号、分号或换行；这些字符用于分隔工艺材料。")
        if name in self.data["materials"]["materials"]:
            raise ValueError(f"材料“{name}”已存在。请换名导入，或选中现有材料进行高级编辑；不会覆盖旧材料。")
        return name

    def _add_material_spec(self, name, spec):
        from ..materials import validate_library
        name = self._check_material_name(name)
        candidate = copy.deepcopy(self.data["materials"])
        candidate["materials"][name] = spec
        validate_library(candidate)
        self.data["materials"]["materials"][name] = spec
        self.refresh_materials(select=name)
        self.changed()
        self.status.set(f"材料“{name}”已加入工程草稿；请在工艺步骤中选择它，然后保存 / 重新生成。")
        return spec

    def delete_material(self):
        self.require_project()
        selected = self.material_tree.selection()
        if not selected:
            raise ValueError("先选中要删除的材料")
        name = selected[0]
        references = self.material_references(name)
        if references:
            raise ValueError(f"不能删除“{name}”，仍被引用：" + "；".join(references) + "。请先在相应工艺或表单中更换材料。")
        if len(self.data["materials"]["materials"]) <= 1:
            raise ValueError("材料库至少保留一种材料。")
        del self.data["materials"]["materials"][name]
        self.refresh_materials()
        self.changed()
        self.status.set(f"已从草稿移除未使用材料“{name}”；源 TXT / CSV 文件保持原样。")

    def import_material(self, path, name, wavelength_unit, *, columns=None, assume_lossless=False, delimiter="auto"):
        name = self._check_material_name(name)
        table = read_material_table(path, delimiter=delimiter)
        spec = material_spec_from_table(table, wavelength_unit=wavelength_unit, columns=columns, assume_lossless=assume_lossless)
        return self._add_material_spec(name, spec)

    def material_import_dialog(self):
        self.require_project()
        path = filedialog.askopenfilename(title="导入本地光学常数", parent=self.winfo_toplevel(), filetypes=[("光学常数文本表", "*.txt *.csv *.dat"), ("所有文件", "*.*")])
        if not path:
            return
        window = tk.Toplevel(self)
        window.title("材料导入 · 明确列与单位")
        window.geometry(f"{min(800, window.winfo_screenwidth() - 80)}x{min(740, window.winfo_screenheight() - 100)}")
        frame = ttk.Frame(window, padding=20)
        frame.pack(fill="both", expand=True)
        variables = {key: tk.StringVar(value=value) for key, value in {"name": Path(path).stem, "unit": "", "wave": "1", "n": "2", "k": "", "delimiter": "自动识别"}.items()}
        ttk.Label(frame, text=str(path), wraplength=730, style="Hint.TLabel").grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        for i, (key, label, options) in enumerate((("name", "新材料名称", None), ("unit", "原文件波长单位（必选）", ["nm", "um", "m"]),
                                                ("wave", "波长列（从 1 开始）", None), ("n", "n 列（从 1 开始）", None), ("k", "k 列（缺少则留空）", None),
                                                ("delimiter", "分隔符", ["自动识别", "逗号 ,", "分号 ;", "空格 / 制表符"]))):
            field(frame, 1 + i // 2, i % 2, label, variables[key], values=options)
        lossless = tk.BooleanVar(value=False)
        lossless_box = ttk.Checkbutton(frame, text="若 k 缺失，由我明确采用 k=0 近似（默认保留未知）", variable=lossless)
        lossless_box.grid(row=4, column=0, columnspan=2, sticky="w", pady=8)
        preview = tk.Text(frame, height=10, wrap="none", font=("Consolas", 10), state="disabled")
        preview.grid(row=5, column=0, columnspan=2, sticky="nsew")
        preview_scroll = ttk.Scrollbar(frame, orient="horizontal", command=preview.xview)
        preview_scroll.grid(row=6, column=0, columnspan=2, sticky="ew")
        preview.configure(xscrollcommand=preview_scroll.set)
        frame.rowconfigure(5, weight=1)
        note = tk.StringVar(value="程序不会猜测波长单位；请核对原文件。最多预览前 8 行。")
        ttk.Label(frame, textvariable=note, wraplength=735, style="Hint.TLabel").grid(row=7, column=0, columnspan=2, sticky="ew", pady=8)
        error = tk.StringVar()
        ttk.Label(frame, textvariable=error, foreground="#b23939", wraplength=735).grid(row=8, column=0, columnspan=2, sticky="ew")
        delimiters = {"自动识别": "auto", "逗号 ,": "comma", "分号 ;": "semicolon", "空格 / 制表符": "whitespace"}
        def read_preview(initial=False):
            table = read_material_table(path, delimiter=delimiters[variables["delimiter"].get()])
            if initial and table.column_count == 3:
                variables["k"].set("3")
            header = "原始列号： " + "\t".join(str(i + 1) for i in range(table.column_count))
            lines = [header, *(["文件表头：" + table.header] if table.header else []),
                     *[f"行 {line}: " + "\t".join(f"{value:.9g}" for value in row)
                       for line, row in zip(table.line_numbers[:8], table.rows[:8])]]
            preview.configure(state="normal")
            preview.delete("1.0", "end")
            preview.insert("1.0", "\n".join(lines))
            preview.configure(state="disabled")
            note.set(f"识别到 {len(table.rows)} 行 × {table.column_count} 列；编码 {table.encoding}；分隔符 {table.delimiter}。\n请核对列号与波长单位。" + ("k 列留空：吸收数据保持未知。" if not variables["k"].get().strip() else ""))
            error.set("")
            return table
        def selected_spec():
            table = read_preview()
            columns = columns_from_fields(*(variables[key].get() for key in ("wave", "n", "k")), table.column_count)
            spec = material_spec_from_table(table, wavelength_unit=variables["unit"].get(), columns=columns, assume_lossless=lossless.get())
            return spec
        def checked_preview():
            spec = selected_spec()
            count = len(spec["wavelength_nm"])
            lo, hi = spec["wavelength_nm"][0], spec["wavelength_nm"][-1]
            k_info = "k 已提供" if spec["k"] is not None else ("k 未知，明确按 0 近似" if spec["assume_lossless"] else "k 未知（不会自动设为 0）")
            note.set(f"校验通过：{count} 个不同波长；换算后范围 {lo:g}～{hi:g} nm；{k_info}。\n波长已排序，完全相同的重复行会合并；冲突重复行会阻止导入。")
        def guarded(action):
            try:
                action()
                error.set("")
            except Exception as exc:
                error.set(str(exc))
        def apply():
            self._check_material_name(variables["name"].get())
            self._add_material_spec(variables["name"].get(), selected_spec())
            window.destroy()
        def k_changed(*_):
            provided = bool(variables["k"].get().strip())
            lossless_box.configure(state="disabled" if provided else "normal")
            if provided:
                lossless.set(False)
        variables["k"].trace_add("write", k_changed)
        bar = ttk.Frame(frame)
        bar.grid(row=9, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(bar, text="取消", command=window.destroy).pack(side="left", padx=4)
        ttk.Button(bar, text="校验选列与单位", command=lambda: guarded(checked_preview)).pack(side="left", padx=4)
        ttk.Button(bar, text="校验并加入工程", command=lambda: guarded(apply), style="Primary.TButton").pack(side="left", padx=4)
        guarded(lambda: read_preview(initial=True))
        k_changed()
        # A modal dialog prevents switching projects while its draft is open.
        make_modal(window, self)
        return window

    def constant_material_dialog(self):
        self.require_project()
        window = tk.Toplevel(self)
        window.title("常数 n / k · 仅在声明范围内有效")
        frame = ttk.Frame(window, padding=20)
        frame.pack(fill="both", expand=True)
        variables = {key: tk.StringVar(value=value) for key, value in {"name": "MY_MATERIAL", "n": "1.5", "k": "0", "lo": "400", "hi": "1000"}.items()}
        for i, (key, label) in enumerate((("name", "新材料名"), ("n", "n（正数）"), ("k", "k（非负）"), ("lo", "范围起点 nm"), ("hi", "范围终点 nm"))):
            field(frame, i, 0, label, variables[key])
        hint_text = "这是在指定范围内 n、k 不随波长变化的近似。k=0 是你明确填写的无吸收值。"
        ttk.Label(frame, text=hint_text, wraplength=420, style="Hint.TLabel").grid(row=5, column=0, sticky="ew", pady=8)
        error = tk.StringVar()
        ttk.Label(frame, textvariable=error, wraplength=420, foreground="#b23939").grid(row=6, column=0, sticky="ew")
        def apply():
            self.add_constant_material(*(variables[key].get() for key in ("name", "n", "k", "lo", "hi")))
            window.destroy()
        def guarded():
            try:
                apply()
            except Exception as exc:
                error.set(str(exc))
        bar = ttk.Frame(frame)
        bar.grid(row=7, column=0, sticky="e", pady=10)
        ttk.Button(bar, text="取消", command=window.destroy).pack(side="left", padx=4)
        ttk.Button(bar, text="加入工程", command=guarded, style="Primary.TButton").pack(side="left", padx=4)
        make_modal(window, self)
        return window

    def add_constant_material(self, name, n, k, lo, hi):
        import math
        name = self._check_material_name(name)
        values = []
        for value, label in zip((n, k, lo, hi), ("n", "k", "波长起点", "波长终点")):
            try:
                number = float(value)
            except (ValueError, TypeError):
                raise ValueError(f"{label} 需要填写数字。") from None
            if not math.isfinite(number):
                raise ValueError(f"{label} 必须是有限数字，不能为 NaN 或无穷大。")
            values.append(number)
        n, k, lo, hi = values
        if n <= 0 or k < 0 or not 0 < lo < hi:
            raise ValueError("要求 n > 0、k ≥ 0、0 < 波长起点 < 终点；波长单位为 nm。")
        return self._add_material_spec(name, {"wavelength_nm": [lo, hi], "n": [n, n], "k": [k, k], "k_status": "provided",
                                             "assume_lossless": False, "source": "explicit_user_constant_index", "valid_range_nm": [lo, hi]})

    def apply_material_edit(self, name, spec):
        from ..materials import validate_library
        self.require_project()
        if name not in self.data["materials"]["materials"]:
            raise ValueError("该材料已不在当前工程中，请重新选择。")
        if not isinstance(spec, dict):
            raise ValueError("材料 JSON 必须是包含 wavelength_nm、n、k 的对象。")
        if spec == self.data["materials"]["materials"][name]:
            return
        library = copy.deepcopy(self.data["materials"])
        spec = copy.deepcopy(spec)
        library["materials"][name] = spec
        validate_library(library)
        # Display/export metadata follows the actual numeric table after edits.
        spec["k_status"] = "unknown" if spec.get("k") is None else "provided"
        if spec.get("k") is not None:
            spec["assume_lossless"] = False
        spec["valid_range_nm"] = [spec["wavelength_nm"][0], spec["wavelength_nm"][-1]]
        spec["edited_in_modeler"] = True
        if spec == self.data["materials"]["materials"][name]:
            return
        self.data["materials"] = library
        self.refresh_materials(select=name)
        self.changed()
        self.status.set(f"材料“{name}”已修改，所有引用该名称的步骤会使用此表；请保存并重新生成结构。")

    def edit_material(self):
        self.require_project()
        selected = self.material_tree.selection()
        if not selected:
            raise ValueError("先选中材料")
        name = selected[0]
        JsonDialog(self, "材料数据 · " + name + "（应用后需保存并重新生成）", self.data["materials"]["materials"][name],
                   lambda spec: self.apply_material_edit(name, spec))
