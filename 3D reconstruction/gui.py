"""Chinese two-stage SEM/TEM desktop workflow. All Tk operations are main-thread."""
from __future__ import annotations

import copy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import traceback
import webbrowser

ROOT = Path(__file__).resolve().parent
DEMO_MODE = "生成配对测试图（无需真实图片）"
IMAGE_MODE = "使用真实 SEM / TEM 图"
MATCH = {"逐条对应": "ordered", "共用代表截面": "median"}
EDGE = {"灰度阈值边缘": "threshold", "SEM 双亮边配对": "bright_rim"}
MISSING = {"保留缺口和无效段": "preserve", "有缺失时拒绝重建": "reject", "显式插值补全（模型假设）": "interpolate"}
PROFILE = {"直侧壁": "linear", "曲线侧壁（含鼓出）": "quadratic"}
POLARITY = {"结构较暗": "dark", "结构较亮": "bright"}


class ScrollFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.content = ttk.Frame(self.canvas, padding=10)
        self.window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window, width=e.width))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class ReconstructionApp(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=8)
        self.master = master
        self.pack(fill="both", expand=True)
        self.messages = queue.Queue()
        self.busy, self.result, self.prepared = False, None, None
        self.prepared_signature, self.result_signature = None, None
        self.cancel_event = threading.Event()
        self.vars, self.current_config = {}, {}
        self.processing = {"sem": {}, "tem": {}}
        self.calibration_confirmed = {"sem": False, "tem": False}
        self.calibration_source = {"sem": "", "tem": ""}
        self.calibration_status = {k: tk.StringVar(value="未校准") for k in ("sem", "tem")}
        self._suspend, self._previous_mode = True, DEMO_MODE
        self._close_requested = False
        self.mode = tk.StringVar(value=DEMO_MODE)
        self.status = tk.StringVar(value="先校准并提取边缘，再核对、分配材料、导出。")
        self.relative_paths, self.advanced_open = tk.BooleanVar(value=True), tk.BooleanVar(value=False)
        self.history, self.preview_images, self.preview_paths = [], {}, {}
        self._preview_after = None
        self._build()
        self._load_defaults()
        self._suspend = False
        self._poll_after = self.after(100, self._poll_messages)

    def _var(self, name):
        if name not in self.vars:
            var = tk.StringVar()
            var.trace_add("write", lambda *_a, field=name: self._field_changed(field))
            self.vars[name] = var
        return self.vars[name]

    def _build(self):
        header = ttk.Frame(self)
        header.pack(fill="x", pady=(0, 6))
        ttk.Label(header, text="SEM / TEM 光栅三维重建", font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        ttk.Button(header, text="加载配置", command=self.load_configuration).pack(side="right", padx=3)
        ttk.Button(header, text="保存配置", command=self.save_configuration).pack(side="right", padx=3)
        ttk.Checkbutton(header, text="保存相对路径", variable=self.relative_paths).pack(side="right", padx=5)
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True)
        self.pages = [ttk.Frame(self.tabs, padding=6) for _ in range(5)]
        for page, text in zip(self.pages, ("1 导入与标尺", "2 边缘核对", "3 材料分区", "4 重建与导出", "5 结果与质量")):
            self.tabs.add(page, text=text)
        for builder, page in zip((self._build_inputs, self._build_review, self._build_materials, self._build_export, self._build_results), self.pages):
            builder(page)
        footer = ttk.Frame(self)
        # Reserve the action bar before the notebook when the window is small.
        footer.pack(side="bottom", fill="x", pady=(6, 0), before=self.tabs)
        self.export_button = ttk.Button(footer, text="重建并导出", command=self.start_reconstruction, state="disabled")
        self.export_button.pack(side="right", padx=4)
        self.run_button = self.export_button
        self.prepare_button = ttk.Button(footer, text="提取并核对", command=self.start_measurements)
        self.prepare_button.pack(side="right", padx=4)
        self.cancel_button = ttk.Button(footer, text="取消", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="right", padx=4)
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=75)
        self.progress.pack(side="right", padx=7)
        status_label = ttk.Label(footer, textvariable=self.status, wraplength=650)
        status_label.pack(side="left", fill="x", expand=True)
        status_label.bind("<Configure>", lambda event: status_label.configure(wraplength=max(200, event.width)))
        self.master.protocol("WM_DELETE_WINDOW", self.close)

    def _path_row(self, parent, row, title, name, choose, readonly=False, display_var=None):
        ttk.Label(parent, text=title).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(parent, textvariable=display_var if display_var is not None else self._var(name), state="readonly" if readonly else "normal").grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(parent, text="选择…", command=choose).grid(row=row, column=2, padx=(7, 0), pady=4)

    def _numbers(self, parent, fields):
        for label, name in fields:
            item = ttk.Frame(parent)
            item.pack(side="left", fill="x", expand=True, padx=(0, 10))
            ttk.Label(item, text=label).pack(anchor="w")
            ttk.Entry(item, textvariable=self._var(name), width=12).pack(fill="x", pady=(3, 0))

    def _combo(self, parent, label, key, options, width=22):
        item = ttk.Frame(parent)
        item.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ttk.Label(item, text=label).pack(anchor="w")
        combo = ttk.Combobox(item, textvariable=self._var(key), values=list(options), width=width, state="readonly")
        combo.pack(fill="x", pady=3)
        return combo

    def _build_inputs(self, frame):
        from grating3d.ui_image import ImageCanvas
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Label(row, text="图片来源").pack(side="left", padx=(0, 10))
        self.mode_combo = ttk.Combobox(row, textvariable=self.mode, values=[DEMO_MODE, IMAGE_MODE], width=32, state="readonly")
        self.mode_combo.pack(side="left")
        self.mode_combo.bind("<<ComboboxSelected>>", self._mode_changed)
        ttk.Label(row, text="演示真值由生成器提供；真实图需重新校准。", wraplength=420).pack(side="left", padx=12)
        self.input_tabs = ttk.Notebook(frame)
        self.input_tabs.pack(fill="both", expand=True, pady=(7, 0))
        self.image_canvases = {}
        self.image_display_paths = {kind: tk.StringVar() for kind in ("sem", "tem")}
        for name, title in (("sem", "SEM 俯视图"), ("tem", "TEM 截面图")):
            page = ttk.Frame(self.input_tabs, padding=4)
            self.input_tabs.add(page, text=title)
            inputs = ttk.Frame(page)
            inputs.pack(fill="x")
            inputs.columnconfigure(1, weight=1)
            self._path_row(inputs, 0, title, f"{name}_image", lambda k=name: self.choose_image(f"{k}_image"), readonly=True, display_var=self.image_display_paths[name])
            cal = ttk.Frame(page)
            cal.pack(fill="x", pady=5)
            self._numbers(cal, [("水平标尺 (nm/像素)", f"{name}_nm_per_pixel"), ("纵向标尺 (nm/像素)", f"{name}_nm_per_pixel_y")])
            ttk.Button(cal, text="采用输入标尺（设备已知值）", command=lambda k=name: self.adopt_calibration(k)).pack(side="left", padx=8, pady=(15, 0))
            ttk.Label(cal, textvariable=self.calibration_status[name], foreground="#9b4f00").pack(side="left", padx=5, pady=(15, 0))
            ttk.Label(page, text="先选择结构 ROI，排除文字和标尺；旋转后对齐。可点击标尺两端并输入长度。TEM 在处理预览中标顶面、基底。", wraplength=1080).pack(anchor="w", pady=(0, 4))
            canvas = ImageCanvas(page, on_change=lambda k=name: self._canvas_changed(k), kind=name.upper())
            canvas.pack(fill="both", expand=True)
            self.image_canvases[name] = canvas

    def _build_review(self, frame):
        from grating3d.ui_image import ImageCanvas
        ttk.Label(frame, text="先核对叠加边缘与实际边界。缺口默认保留；TEM 保护层应通过顶面和基底标记排除。修改图像设置后需再次提取。", wraplength=1100).pack(anchor="w", pady=(0, 5))
        controls = ttk.Frame(frame)
        controls.pack(fill="x")
        self._combo(controls, "SEM 边缘识别", "sem_edge_mode", EDGE)
        self._combo(controls, "SEM 缺失段处理", "missing_policy", MISSING, width=27)
        self._combo(controls, "TEM 侧壁模型", "sidewall_model", PROFILE)
        self._combo(controls, "截面与俯视对应", "tem_match", MATCH)
        polarities = ttk.Frame(frame)
        polarities.pack(fill="x", pady=(1, 4))
        self._combo(polarities, "SEM 灰度", "sem_polarity", POLARITY)
        self._combo(polarities, "TEM 灰度", "tem_polarity", POLARITY)
        self._numbers(polarities, [("额外侧壁鼓出 (nm，0=仅用TEM)", "bowing_nm")])
        self.review_summary = scrolledtext.ScrolledText(frame, height=3, font=("Microsoft YaHei UI", 10), wrap="word", state="disabled")
        self.review_summary.pack(fill="x", pady=4)
        warnings_bar = ttk.Frame(frame)
        warnings_bar.pack(fill="x", pady=(0, 4))
        self.review_quality_summary = tk.StringVar(value="提取后显示边缘缺失、拟合残差和对应关系提示。")
        ttk.Label(warnings_bar, textvariable=self.review_quality_summary, foreground="#986000", wraplength=900).pack(side="left", fill="x", expand=True)
        ttk.Button(warnings_bar, text="完整质量提示", command=lambda: self.tabs.select(4)).pack(side="right", padx=5)
        self.review_tabs = ttk.Notebook(frame)
        self.review_tabs.pack(fill="both", expand=True)
        self.review_canvases = {}
        for name, title in (("sem", "SEM 边缘与缺口"), ("tem", "TEM 轮廓与拟合")):
            page = ttk.Frame(self.review_tabs)
            self.review_tabs.add(page, text=title)
            canvas = ImageCanvas(page, on_change=lambda k=name: self._canvas_changed(k, review=True), kind=name.upper())
            canvas.pack(fill="both", expand=True)
            self.review_canvases[name] = canvas

    def _build_materials(self, frame):
        from grating3d.ui_regions import RegionEditor
        ttk.Label(frame, text="材料由你指定；显微图灰度不直接等于材料。坐标单位 nm，z=0 是基底上表面。覆盖优先级越高，越后赋值。", wraplength=1100).pack(anchor="w", pady=(0, 5))
        library = ttk.Frame(frame)
        library.pack(fill="x")
        library.columnconfigure(1, weight=1)
        self._path_row(library, 0, "材料库", "material_library", self.choose_library)
        tools = ttk.Frame(frame)
        tools.pack(fill="x", pady=4)
        self.background_combo = self._combo(tools, "未指定区域 / 背景材料", "background_material", [])
        self._numbers(tools, [("光学查询波长 (nm，演示假定633)", "wavelength_nm")])
        ttk.Button(tools, text="更新 n、k 与介电率", command=lambda: self.refresh_materials(show_error=True)).pack(side="left", padx=7, pady=(14, 0))
        self.material_table = ttk.Treeview(frame, columns=("n", "k", "er", "ei", "range"), show="tree headings", height=4)
        self.material_table.heading("#0", text="材料 ID / 常见名称")
        self.material_table.column("#0", width=145)
        for key, label, width in (("n", "n", 95), ("k", "k（吸收）", 95), ("er", "εr 实部", 105), ("ei", "εr 虚部", 105), ("range", "有效波长 nm", 145)):
            self.material_table.heading(key, text=label)
            self.material_table.column(key, width=width)
        self.material_table.pack(fill="x", pady=4)
        self.material_basis = tk.StringVar(value="选择查询波长后显示当前 n、k 和介电率。")
        ttk.Label(frame, textvariable=self.material_basis, foreground="#986000", wraplength=1100).pack(anchor="w", pady=(0, 4))
        self.region_editor = RegionEditor(frame, materials=[], on_change=self._materials_changed)
        self.region_editor.pack(fill="both", expand=True)
        ttk.Label(frame, text="εr(λ)=(n+ik)²。区域是否实际覆盖到结构，会在导出后的质量页显示。", wraplength=1100).pack(anchor="w", pady=4)

    def _build_export(self, frame):
        scroll = ScrollFrame(frame)
        scroll.pack(fill="both", expand=True)
        body = scroll.content
        ttk.Label(body, text="核对边缘后，导出三维体素、材料 ID 与介电率 / 色散参数。\n未知波长时先完成提取核对；正式导出需指定波长。", wraplength=1050).pack(anchor="w", pady=6)
        output = ttk.LabelFrame(body, text="每次运行保存到独立子目录，保留历史结果", padding=10)
        output.pack(fill="x", pady=5)
        output.columnconfigure(1, weight=1)
        self._path_row(output, 0, "输出根目录", "output_dir", self.choose_output)
        grid = ttk.LabelFrame(body, text="重建范围与网格", padding=10)
        grid.pack(fill="x", pady=5)
        line = ttk.Frame(grid)
        line.pack(fill="x")
        self._numbers(line, [("基底厚度 (nm)", "substrate_depth_nm"), ("上方空气余量 (nm)", "air_padding_nm")])
        line = ttk.Frame(grid)
        line.pack(fill="x", pady=8)
        self._numbers(line, [("体素 x 间距 (nm)", "dx"), ("体素 y 间距 (nm)", "dy"), ("体素 z 间距 (nm)", "dz")])
        self.resource_summary = tk.StringVar(value="完成边缘提取后估算网格规模。")
        ttk.Label(grid, textvariable=self.resource_summary, wraplength=1040).pack(anchor="w", pady=4)
        ttk.Checkbutton(body, text="高级参数与配置", variable=self.advanced_open, command=self._toggle_advanced).pack(anchor="w", pady=8)
        self.advanced_frame = ttk.LabelFrame(body, text="高级选项（通常无需修改）", padding=10)
        row = ttk.Frame(self.advanced_frame)
        row.pack(fill="x")
        self._numbers(row, [("最多体素数", "max_voxels"), ("SEM 平滑 σ (像素)", "sem_sigma"), ("TEM 平滑 σ (像素)", "tem_sigma")])
        self.export_csv = tk.BooleanVar(value=False)
        self.export_csv.trace_add("write", lambda *_a: self._mark_result_stale("导出选项已修改"))
        ttk.Checkbutton(self.advanced_frame, text="额外导出逐体素 CSV（文件可能很大）", variable=self.export_csv).pack(anchor="w", pady=7)
        ttk.Label(self.advanced_frame, text="完整配置可保存为 JSON；材料日常编辑使用表单。体素间距属于离散化设置，不代表显微测量精度。", wraplength=1040).pack(anchor="w")
        ttk.Label(body, text="二维图像之外的侧壁沿光栅方向延拓，需要模型假设。此窗口只用于图像核对和几何重建；保存标注配置后回到 MarkSimModeler，在 SEM/TEM 页载入配置，再由统一导出页输出结构包与 Lumerical 形状。这里不运行电磁求解器。", wraplength=1050).pack(anchor="w", pady=12)

    def _build_results(self, frame):
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 5))
        for title, callback in (("打开输出目录", self.open_output), ("交互三维预览", self.open_interactive), ("完整报告", self.open_report)):
            ttk.Button(bar, text=title, command=callback).pack(side="left", padx=(0, 5))
        self.history_choice = tk.StringVar()
        self.history_combo = ttk.Combobox(bar, textvariable=self.history_choice, state="readonly", width=27)
        self.history_combo.pack(side="right", fill="x", expand=True, padx=(8, 0))
        self.history_combo.bind("<<ComboboxSelected>>", self._select_history)
        self.result_state = tk.StringVar(value="尚未生成三维结果。先提取核对边缘，再重建并导出。")
        ttk.Label(frame, textvariable=self.result_state, foreground="#986000", wraplength=1100).pack(anchor="w", pady=(0, 3))
        self.measurements = scrolledtext.ScrolledText(frame, height=3, font=("Microsoft YaHei UI", 10), wrap="word", state="disabled")
        self.measurements.pack(fill="x", pady=4)
        self.quality = scrolledtext.ScrolledText(frame, height=5, font=("Microsoft YaHei UI", 10), wrap="word", state="disabled")
        for name, color in (("warning", "#986000"), ("error", "#b32d2d"), ("info", "#315970")):
            self.quality.tag_configure(name, foreground=color)
        self.quality.pack(fill="x", pady=4)
        self.previews = ttk.Notebook(frame)
        self.previews.pack(fill="both", expand=True)
        self.preview_labels = {}
        for key, title in (("geometry_preview", "三维形貌"), ("material_slices", "材料切片"), ("sem_edges", "SEM 测量图"), ("tem_edges", "TEM 测量图")):
            page = ttk.Frame(self.previews)
            self.previews.add(page, text=title)
            label = ttk.Label(page, text="完成导出后显示", anchor="center")
            label.pack(fill="both", expand=True, padx=3, pady=3)
            label.bind("<Configure>", self._schedule_preview_resize)
            self.preview_labels[key] = label
        self.log = scrolledtext.ScrolledText(frame, height=4, font=("Consolas", 9), wrap="word", state="disabled")
        self.log.pack(fill="x", pady=(5, 0))

    def _toggle_advanced(self):
        if self.advanced_open.get():
            self.advanced_frame.pack(fill="x", pady=4)
        else:
            self.advanced_frame.pack_forget()

    def _load_defaults(self):
        from grating3d.pipeline import demo_config
        self._apply_config(demo_config(ROOT / "outputs" / "gui_runs"))

    @staticmethod
    def _label(mapping, value):
        return next((label for label, internal in mapping.items() if internal == value), next(iter(mapping)))

    def _apply_config(self, config):
        suspended, self._suspend = self._suspend, True
        self.current_config = copy.deepcopy(config)
        self.mode.set(DEMO_MODE if config.get("generate_demo") else IMAGE_MODE)
        self._previous_mode = self.mode.get()
        for name in ("sem_image", "tem_image", "material_library", "output_dir", "wavelength_nm", "background_material"):
            self._var(name).set("" if config.get(name) is None else str(config.get(name, "")))
        for name in ("sem", "tem"):
            self.image_display_paths[name].set(str(config.get(f"{name}_image", "")))
            self.processing[name] = copy.deepcopy(config.get(f"{name}_processing", {}))
            entry = config.get("calibration", {}).get(name, {})
            scale = config.get(f"{name}_nm_per_pixel", 3 if config.get("generate_demo") else "")
            if isinstance(scale, (list, tuple)):
                sx, sy = scale
            elif isinstance(scale, dict):
                sx, sy = scale.get("x", scale.get("nm_per_pixel_x", "")), scale.get("y", scale.get("nm_per_pixel_y", ""))
            else:
                sx, sy = scale, config.get(f"{name}_nm_per_pixel_y", scale)
            sx, sy = entry.get("nm_per_pixel_x", sx), entry.get("nm_per_pixel_y", sy)
            self._var(f"{name}_nm_per_pixel").set("" if sx is None else str(sx))
            self._var(f"{name}_nm_per_pixel_y").set("" if sy is None else str(sy))
            self.calibration_confirmed[name] = bool(config.get("generate_demo") or entry.get("confirmed"))
            self.calibration_source[name] = "synthetic" if config.get("generate_demo") else entry.get("source", "")
            self._update_calibration_status(name)
            path = config.get(f"{name}_image")
            if path and Path(path).is_file():
                for canvas in (self.image_canvases[name], self.review_canvases[name]):
                    canvas.set_image(path)
                    canvas.set_settings(self.processing[name])
                    if self.calibration_confirmed[name]:
                        canvas.set_calibration(float(sx), float(sy), source=self.calibration_source[name])
        grid = config.get("grid", {})
        spacing = grid.get("spacing_nm", [15, 15, 10])
        if isinstance(spacing, (int, float)):
            spacing = [spacing] * 3
        for name, value in zip(("dx", "dy", "dz"), spacing):
            self._var(name).set(str(value))
        for key, default in (("substrate_depth_nm", 200), ("air_padding_nm", 250), ("max_voxels", 12000000)):
            self._var(key).set(str(grid.get(key, default)))
        geometry = config.get("geometry", {})
        self._var("bowing_nm").set(str(geometry.get("bowing_nm", 0)))
        self._var("tem_match").set(self._label(MATCH, geometry.get("tem_match", "ordered")))
        self._var("sem_edge_mode").set(self._label(EDGE, self.processing["sem"].get("edge_mode", "threshold")))
        self._var("missing_policy").set(self._label(MISSING, self.processing["sem"].get("missing_policy", "preserve")))
        self._var("sidewall_model").set(self._label(PROFILE, self.processing["tem"].get("sidewall_model", "linear")))
        for name in ("sem", "tem"):
            self._var(f"{name}_polarity").set(self._label(POLARITY, self.processing[name].get("polarity", "dark")))
            self._var(f"{name}_sigma").set(str(self.processing[name].get("smooth_sigma_px", 0.8)))
        self.region_editor.set_regions(config.get("regions", []))
        self.export_csv.set(bool(config.get("export_voxel_csv", config.get("export_csv", False))))
        self.refresh_materials(show_error=False)
        self._suspend = suspended
        self._invalidate_measurements("配置已载入，请提取并核对边缘")

    def _field_changed(self, field):
        if self._suspend:
            return
        if field.startswith(("sem_nm_per_pixel", "tem_nm_per_pixel")):
            name = field.split("_")[0]
            self.calibration_confirmed[name] = False
            self.calibration_source[name] = ""
            for canvas in (self.image_canvases[name], self.review_canvases[name]):
                canvas.clear_calibration()
            self._update_calibration_status(name)
        image_fields = {"sem_nm_per_pixel", "sem_nm_per_pixel_y", "tem_nm_per_pixel", "tem_nm_per_pixel_y", "sem_edge_mode", "sem_polarity", "tem_polarity", "missing_policy", "sidewall_model", "tem_match", "bowing_nm", "sem_sigma", "tem_sigma"}
        if field in image_fields:
            self._invalidate_measurements("图像或测量设置已改变，请重新提取")
        else:
            self._mark_result_stale("参数已修改，旧结果未重算")
            if field in {"wavelength_nm", "material_library"}:
                self.material_basis.set("查询参数已修改：点击‘更新 n、k 与介电率’刷新表格。导出使用输入的当前波长。")
            if field in {"dx", "dy", "dz", "substrate_depth_nm", "air_padding_nm", "max_voxels"}:
                self.region_editor.set_coverage([])
                self._update_resources()

    def _mode_changed(self, _event=None):
        if self._suspend or self.mode.get() == self._previous_mode:
            return
        self._suspend = True
        for name in ("sem", "tem"):
            self.processing[name] = {}
            if self.mode.get() == IMAGE_MODE:
                for key in (f"{name}_nm_per_pixel", f"{name}_nm_per_pixel_y"):
                    self._var(key).set("")
                self.calibration_confirmed[name], self.calibration_source[name] = False, ""
                for canvas in (self.image_canvases[name], self.review_canvases[name]):
                    path = self._var(f"{name}_image").get()
                    if path and Path(path).is_file():
                        canvas.set_image(path, reset=True)
            else:
                self._var(f"{name}_nm_per_pixel").set("3")
                self._var(f"{name}_nm_per_pixel_y").set("3")
                self.calibration_confirmed[name], self.calibration_source[name] = True, "synthetic"
                for canvas in (self.image_canvases[name], self.review_canvases[name]):
                    canvas.set_settings({"rotation_deg": 0})
                    canvas.set_calibration(3, 3, source="synthetic")
            self._update_calibration_status(name)
        if self.mode.get() == DEMO_MODE:
            self._var("sem_edge_mode").set(self._label(EDGE, "threshold"))
            self._var("sidewall_model").set(self._label(PROFILE, "linear"))
            self._var("missing_policy").set(self._label(MISSING, "preserve"))
            self._var("sem_polarity").set(self._label(POLARITY, "dark"))
            self._var("tem_polarity").set(self._label(POLARITY, "dark"))
            if not self._var("wavelength_nm").get().strip():
                self._var("wavelength_nm").set("633")
        elif self._previous_mode == DEMO_MODE:
            # The demonstration wavelength is an assumption, not a real acquisition setting.
            self._var("wavelength_nm").set("")
        self._suspend, self._previous_mode = False, self.mode.get()
        self._invalidate_measurements("来源已改变：真实图片需校准；演示标尺由生成器确定")

    def choose_image(self, name):
        path = filedialog.askopenfilename(title="选择显微图像", initialdir=ROOT, filetypes=[("图像", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp"), ("全部", "*.*")])
        if path:
            self.set_real_image(name.split("_")[0], path)

    def set_real_image(self, name, path):
        if self.busy:
            self.status.set("当前步骤结束或取消后，可更换图片。")
            return
        self.mode.set(IMAGE_MODE)
        self._mode_changed()
        self._suspend = True
        self._var(f"{name}_image").set(str(Path(path).resolve()))
        self.image_display_paths[name].set(str(Path(path).resolve()))
        self._var(f"{name}_nm_per_pixel").set("")
        self._var(f"{name}_nm_per_pixel_y").set("")
        self.processing[name] = {}
        self.calibration_confirmed[name], self.calibration_source[name] = False, ""
        for canvas in (self.image_canvases[name], self.review_canvases[name]):
            canvas.set_image(path, reset=True)
        self._suspend = False
        self._update_calibration_status(name)
        self._invalidate_measurements(f"已导入 {name.upper()}：请点击标尺或采用设备已知标尺")

    def adopt_calibration(self, name):
        try:
            sx = self._number(self._var(f"{name}_nm_per_pixel").get(), f"{name.upper()} 水平标尺")
            sy = self._number(self._var(f"{name}_nm_per_pixel_y").get(), f"{name.upper()} 纵向标尺")
        except ValueError as exc:
            messagebox.showerror("标尺需要检查", str(exc))
            return
        self._suspend = True
        self.calibration_confirmed[name] = True
        self.calibration_source[name] = "synthetic" if self.mode.get() == DEMO_MODE else "manual"
        for canvas in (self.image_canvases[name], self.review_canvases[name]):
            canvas.set_calibration(sx, sy, source=self.calibration_source[name])
        self._suspend = False
        self._update_calibration_status(name)
        self._invalidate_measurements(f"已采用 {name.upper()} 标尺；请提取核对")

    def _update_calibration_status(self, name):
        source = {"synthetic": "生成器真值", "manual": "设备已知值", "scale_bar": "图像标尺"}.get(self.calibration_source[name], "配置标尺")
        self.calibration_status[name].set(f"已校准 · {source}" if self.calibration_confirmed[name] else "未校准 · 填写后点击采用")

    def _canvas_changed(self, name, review=False):
        if self._suspend:
            return
        canvas = (self.review_canvases if review else self.image_canvases)[name]
        annotation_keys = {"roi_px", "rotation_deg", "exclude_regions_px", "substrate_row_px", "top_row_px", "substrate_line_px"}
        self.processing[name] = {**{key: value for key, value in self.processing[name].items() if key not in annotation_keys}, **canvas.get_settings()}
        cal = canvas.get_calibration()
        self._suspend = True
        if cal.get("confirmed"):
            sx, sy = cal.get("nm_per_pixel_x", cal.get("sx")), cal.get("nm_per_pixel_y", cal.get("sy"))
            self._var(f"{name}_nm_per_pixel").set(f"{sx:.8g}")
            self._var(f"{name}_nm_per_pixel_y").set(f"{sy:.8g}")
            self.calibration_confirmed[name] = True
            self.calibration_source[name] = cal.get("source", "scale_bar")
        other = (self.image_canvases if review else self.review_canvases)[name]
        other.set_settings(self.processing[name])
        if cal.get("confirmed"):
            other.set_calibration(sx, sy, source=self.calibration_source[name])
        self._suspend = False
        self._update_calibration_status(name)
        self._invalidate_measurements("图像选择 / 标记已修改，请重新提取")

    def choose_library(self):
        path = filedialog.askopenfilename(title="选择材料库", initialdir=ROOT / "materials", filetypes=[("JSON", "*.json")])
        if path:
            self._var("material_library").set(path)
            self.refresh_materials(show_error=True)

    def choose_output(self):
        path = filedialog.askdirectory(title="选择输出根目录", initialdir=ROOT / "outputs")
        if path:
            self._var("output_dir").set(path)

    @staticmethod
    def _number(raw, label, positive=True):
        try:
            value = float(raw)
        except (ValueError, TypeError):
            raise ValueError(f"{label} 必须填写数值") from None
        if not math.isfinite(value) or (positive and value <= 0):
            raise ValueError(f"{label} 必须是{'正的' if positive else ''}有限数值")
        return value

    def _collect_config(self, for_export=True, for_save=False):
        config = copy.deepcopy(self.current_config)
        for name in ("sem_image", "tem_image", "material_library", "output_dir"):
            raw = self._var(name).get().strip()
            path = Path(raw) if raw else None
            config[name] = str(path.resolve() if path.is_absolute() else (ROOT / path).resolve()) if path else ""
        config["generate_demo"], config["calibration"] = self.mode.get() == DEMO_MODE, {}
        for name in ("sem", "tem"):
            if not self.calibration_confirmed[name] and not for_save:
                raise ValueError(f"{name.upper()} 尚未校准。请点击图像标尺，或填写水平 / 纵向值后点击‘采用输入标尺’。")
            sxraw, syraw = self._var(f"{name}_nm_per_pixel").get(), self._var(f"{name}_nm_per_pixel_y").get()
            if for_save and (not sxraw.strip() or not syraw.strip()):
                sx, sy = None, None
            else:
                sx = self._number(sxraw, f"{name.upper()} 水平标尺")
                sy = self._number(syraw, f"{name.upper()} 纵向标尺")
            config[f"{name}_nm_per_pixel"], config[f"{name}_nm_per_pixel_y"] = sx, sy
            config["calibration"][name] = {"confirmed": self.calibration_confirmed[name], "source": self.calibration_source[name], "nm_per_pixel_x": sx, "nm_per_pixel_y": sy}
            processing = copy.deepcopy(self.processing[name])
            processing["polarity"] = POLARITY.get(self._var(f"{name}_polarity").get(), "dark")
            processing["smooth_sigma_px"] = self._number(self._var(f"{name}_sigma").get(), "图像平滑 σ", positive=False)
            if processing["smooth_sigma_px"] < 0:
                raise ValueError("图像平滑 σ 不能为负值")
            config[f"{name}_processing"] = processing
        config["sem_processing"].update(edge_mode=EDGE.get(self._var("sem_edge_mode").get(), "threshold"), missing_policy=MISSING.get(self._var("missing_policy").get(), "preserve"))
        config["tem_processing"]["sidewall_model"] = PROFILE.get(self._var("sidewall_model").get(), "linear")
        if not config["generate_demo"]:
            config.pop("truth_file", None)
            if not for_save:
                for name in ("sem_image", "tem_image"):
                    if not config[name] or not Path(config[name]).is_file():
                        raise ValueError(f"请选择有效的 {name.upper().replace('_IMAGE', '')} 图像")
            config["calibration_note"] = "Real image calibration explicitly adopted in UI; inspect scale and TEM section correspondence."
        else:
            config.setdefault("synthetic", {}).update(sem_nm_per_pixel=config["sem_nm_per_pixel"], tem_nm_per_pixel=config["tem_nm_per_pixel"])
            config["calibration_note"] = "Synthetic calibration supplied by generator; extraction does not read truth."
        config.setdefault("geometry", {}).update(tem_match=MATCH.get(self._var("tem_match").get(), "ordered"), bowing_nm=self._number(self._var("bowing_nm").get(), "额外侧壁鼓出", positive=False))
        config["background_material"] = self._var("background_material").get().strip()
        if for_export or for_save:
            config["regions"] = self.region_editor.get_regions()
            limit = self._number(self._var("max_voxels").get(), "最多体素数")
            if limit != int(limit):
                raise ValueError("最多体素数必须是正整数")
            config.setdefault("grid", {}).update(
                spacing_nm=[self._number(self._var(n).get(), f"体素 {n[1]} 间距") for n in ("dx", "dy", "dz")],
                substrate_depth_nm=self._number(self._var("substrate_depth_nm").get(), "基底厚度"),
                air_padding_nm=self._number(self._var("air_padding_nm").get(), "空气余量"), max_voxels=int(limit))
        wave = self._var("wavelength_nm").get().strip()
        if for_export:
            config["wavelength_nm"] = self._number(wave, "导出波长（仅提取边缘可以暂不填）")
        else:
            try:
                config["wavelength_nm"] = self._number(wave, "波长") if wave else None
            except ValueError:
                config["wavelength_nm"] = None
        config["export_voxel_csv"] = self.export_csv.get()
        config.pop("export_csv", None)
        return config

    @staticmethod
    def _measurement_signature(config):
        keys = ("generate_demo", "sem_image", "tem_image", "sem_nm_per_pixel", "sem_nm_per_pixel_y", "tem_nm_per_pixel", "tem_nm_per_pixel_y", "calibration", "sem_processing", "tem_processing", "geometry", "synthetic")
        return json.dumps({key: config.get(key) for key in keys}, sort_keys=True, ensure_ascii=False)

    def _invalidate_measurements(self, reason):
        if self._suspend:
            return
        self.prepared, self.prepared_signature = None, None
        if hasattr(self, "export_button"):
            self.export_button.configure(state="disabled")
        if hasattr(self, "region_editor"):
            self.region_editor.set_coverage([])
        if hasattr(self, "review_canvases"):
            for canvas in list(self.review_canvases.values()) + list(self.image_canvases.values()):
                canvas.set_overlay(None)
            self._set_text(self.review_summary, "图像 / 测量设置已改变；请重新点击‘提取并核对’，显示新的边缘与尺寸。")
            self.review_quality_summary.set("当前边缘核对结果未更新。历史三维结果保留在结果页。")
        self.status.set(reason)
        self._mark_result_stale(reason)

    def _mark_result_stale(self, reason):
        if not self._suspend and self.result:
            self.status.set(f"{reason}；当前预览是历史结果。")
            self.result_state.set("当前输入已改变；以下预览对应已保存的历史运行，需要重新计算后更新。")

    def _materials_changed(self):
        self.region_editor.set_coverage([])
        self._mark_result_stale("材料区域已修改，需要重新导出计算覆盖体素")

    def load_configuration(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(title="加载重建配置", initialdir=ROOT / "configs", filetypes=[("JSON", "*.json")])
        if path:
            try:
                from grating3d.pipeline import load_config
                self._apply_config(load_config(path))
                self.status.set("配置已载入；旧版真实图标尺需点击采用后生效。")
            except Exception as exc:
                messagebox.showerror("配置加载失败", str(exc))

    def save_configuration(self):
        try:
            config = self._collect_config(for_export=False, for_save=True)
        except Exception as exc:
            messagebox.showerror("配置需要检查", str(exc))
            return
        path = filedialog.asksaveasfilename(title="保存当前配置", initialdir=ROOT / "configs", defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            if self.relative_paths.get():
                for key in ("sem_image", "tem_image", "material_library", "output_dir", "truth_file"):
                    if config.get(key):
                        try:
                            config[key] = Path(os.path.relpath(config[key], Path(path).parent)).as_posix()
                        except ValueError:
                            pass
            Path(path).write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            self.status.set(f"配置已保存：{path}")
        except Exception as exc:
            messagebox.showerror("配置保存失败", str(exc))

    def refresh_materials(self, show_error=False):
        try:
            from grating3d.materials import MaterialLibrary
            raw = self._var("material_library").get().strip()
            library = MaterialLibrary(Path(raw) if Path(raw).is_absolute() else ROOT / raw)
            wave = self._var("wavelength_nm").get().strip()
            wavelength = self._number(wave, "查询波长") if wave else None
            for item in self.material_table.get_children():
                self.material_table.delete(item)
            for name in library.names:
                interval = library.metadata(name).get("valid_range_nm")
                optical = ["待输入波长"] * 4
                if wavelength is not None:
                    try:
                        values = library.evaluate(name, [wavelength])
                        optical = [f"{float(values[k][0]):.5g}" for k in ("n", "k", "epsilon_real", "epsilon_imag")]
                    except ValueError:
                        optical = ["超出范围"] * 4
                aliases = {"air": "空气", "silica": "SiO₂", "silicon": "硅", "silicon_nitride": "Si₃N₄", "photoresist": "光刻胶", "aluminum": "铝"}
                self.material_table.insert("", "end", text=f"{name} · {aliases[name]}" if name in aliases else name, values=[*optical, f"{interval[0]:g}–{interval[1]:g}" if interval else "常数模型"])
            self.background_combo.configure(values=library.names)
            self.region_editor.set_materials(library.names)
            self.material_basis.set(f"当前表格查询波长：{wavelength:g} nm；仿真导出使用当前输入波长。" if wavelength is not None else "尚未指定波长：可以先提取边缘，选定波长后再查询或导出光学参数。")
            if self._var("background_material").get() not in library.names:
                self._var("background_material").set(library.names[0])
            return library
        except Exception as exc:
            if show_error:
                messagebox.showerror("材料库或波长需要检查", str(exc))
            return None

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", str(text) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _run_folder(self):
        raw = self._var("output_dir").get().strip()
        if not raw:
            raise ValueError("请选择输出根目录")
        base = Path(raw)
        if not base.is_absolute():
            base = ROOT / base
        return base / datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")

    def _begin(self, stage, config, signature=None):
        self.busy, self.cancel_event = True, threading.Event()
        self.active_config = copy.deepcopy(config)
        self.prepare_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress.start(15)
        self.status.set("正在提取图像；完成后检查边缘。" if stage == "prepare" else "正在重建、分配材料并导出。")
        self._append_log("开始图像提取。" if stage == "prepare" else "开始导出；复用已核对的测量结果。")
        threading.Thread(target=self._worker, args=(stage, config, signature, self.cancel_event, self.prepared), daemon=True, name=f"grating-{stage}").start()

    def start_measurements(self):
        if self.busy:
            return
        try:
            config = self._collect_config(for_export=False)
            config["output_dir"] = str(self._run_folder())
            signature = self._measurement_signature(config)
        except Exception as exc:
            messagebox.showerror("图像输入需要检查", str(exc))
            return
        self._begin("prepare", config, signature)

    def start_reconstruction(self):
        if self.busy:
            return
        try:
            config = self._collect_config(for_export=True)
            if self.prepared is None or self.prepared_signature != self._measurement_signature(config):
                raise ValueError("请先点击‘提取并核对’，完成后检查边缘叠加图。")
            from grating3d.materials import MaterialLibrary
            library = MaterialLibrary(config["material_library"])
            for name in [config["background_material"], *[r["material"] for r in config["regions"]]]:
                library.evaluate(name, [config["wavelength_nm"]])
            config["output_dir"] = str(self._run_folder())
        except Exception as exc:
            messagebox.showerror("导出输入需要检查", str(exc))
            return
        self._begin("export", config, self.prepared_signature)

    def _worker(self, stage, config, signature, cancel_event, prepared):
        try:
            from grating3d.pipeline import prepare_measurements, run_pipeline
            log = lambda text: self.messages.put(("log", text))
            if stage == "prepare":
                result = prepare_measurements(config, log=log, cancel_event=cancel_event)
                self.messages.put(("prepared", (result, signature, config)))
            else:
                result = run_pipeline(config, log=log, cancel_event=cancel_event, measurements=prepared)
                self.messages.put(("complete", (result, signature)))
        except Exception as exc:
            cancelled = cancel_event.is_set() or exc.__class__.__name__ in {"PipelineCancelled", "CancelledError"}
            self.messages.put(("cancelled" if cancelled else "error", (str(exc), traceback.format_exc())))

    def cancel(self):
        if self.busy:
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status.set("已请求取消；当前阶段安全结束后停止。部分输出不会标为完成。")

    def _poll_messages(self):
        try:
            while True:
                event, payload = self.messages.get_nowait()
                if event == "log":
                    self._append_log(payload)
                    for prefix, text in (("Generating", "生成配对测试图…"), ("Extracting", "提取边缘与截面…"), ("Building", "建立三维模型…"), ("Assigning", "分配三维材料…"), ("Exporting", "导出几何与材料文件…"), ("Writing", "写出结果文件…")):
                        if str(payload).startswith(prefix):
                            self.status.set(text)
                            break
                elif event == "prepared":
                    result, signature, config = payload
                    self._finish()
                    if signature != self._measurement_signature(self._collect_config(for_export=False)):
                        self._invalidate_measurements("提取过程中设置已改变，请按新设置重新提取")
                        continue
                    self.prepared, self.prepared_signature = result, signature
                    self.export_button.configure(state="normal")
                    self._show_prepared(result)
                    self.tabs.select(1)
                    self.status.set("边缘已提取，请核对后进入材料分区并导出。")
                elif event == "complete":
                    result, signature = payload
                    self._finish()
                    self.result, self.result_signature = result, signature
                    self.history.append(result)
                    values = [Path(r["output_dir"]).name for r in self.history]
                    self.history_combo.configure(values=values)
                    self.history_choice.set(values[-1])
                    self._show_result(result)
                    self.tabs.select(4)
                    self.status.set(f"已导出：{Path(result['output_dir']).name}；请检查质量提示和材料覆盖。")
                    try:
                        current = self._collect_config(for_export=True)
                        active = copy.deepcopy(self.active_config)
                        current.pop("output_dir", None)
                        active.pop("output_dir", None)
                        if current != active:
                            self.status.set("本次结果已保存；运行期间参数发生改变，当前预览对应运行开始时的设置。")
                            self.result_state.set("运行期间输入已改变；以下结果对应本次运行开始时的设置。")
                    except ValueError:
                        self.status.set("本次结果已保存；当前输入已改变，预览是历史结果。")
                        self.result_state.set("当前输入已改变；以下预览对应已保存的历史运行。")
                elif event == "cancelled":
                    self._finish()
                    self.status.set("已取消。历史结果保留；本次部分输出不是正式结果。")
                    self._append_log("任务已取消：" + payload[0])
                elif event == "error":
                    self._finish()
                    self.status.set("此步骤失败；按提示修正输入后重试。")
                    self._append_log(payload[1])
                    if not self._close_requested:
                        messagebox.showerror("处理失败", self._translate_error(payload[0]))
        except queue.Empty:
            pass
        except Exception as exc:
            self._finish()
            self._append_log(traceback.format_exc())
            self.status.set(f"结果显示需要检查：{exc}")
        self._poll_after = self.after(100, self._poll_messages)

    def _finish(self):
        self.busy = False
        self.prepare_button.configure(state="normal")
        self.export_button.configure(state="normal" if self.prepared is not None else "disabled")
        self.cancel_button.configure(state="disabled")
        self.progress.stop()
        if self._close_requested:
            self.after_idle(self.master.destroy)

    @staticmethod
    def _translate_error(text):
        text = str(text)
        common = {
            "Insufficient image contrast to identify edges": "图像对比度不足，无法定位边缘。请检查16-bit图像、结构裁切范围以及灰度方向。",
            "Sidewall model makes neighboring ridges overlap": "侧壁模型导致相邻光栅相互重叠。请核对 TEM 侧壁、SEM 线宽、两个标尺以及额外鼓出参数。",
            "Sidewall model creates zero/negative ridge width": "侧壁模型出现零宽度或负宽度。请核对轮廓与侧壁鼓出参数。",
        }
        if text.startswith("SEM edge tracking coverage is insufficient"):
            return "SEM 边缘测量覆盖不足。请检查 ROI、光栅方向、灰度方向、局部缺陷或双亮边模式；原始诊断已记录在日志中。"
        return common.get(text, text)

    @staticmethod
    def _format_metrics(sem, tem):
        def num(value):
            return "—" if value is None else f"{float(value):.1f}"
        return (f"光栅 {sem.get('ridge_count', '—')} 条；周期 {num(sem.get('pitch_nm'))} nm；顶部线宽 {num(sem.get('top_cd_mean_nm'))} nm\n"
                f"高度 {num(tem.get('height_mean_nm'))} nm；底部线宽 {num(tem.get('bottom_cd_mean_nm'))} nm；侧壁角 {num(tem.get('left_angle_mean_deg'))}° / {num(tem.get('right_angle_mean_deg'))}°\n"
                f"周期离散度 {num(sem.get('pitch_std_nm'))} nm；线宽离散度 {num(sem.get('top_cd_std_nm'))} nm（离散度不等于测量不确定度）")

    @staticmethod
    def _set_text(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _show_prepared(self, prepared):
        self._suspend = True
        for name in ("sem", "tem"):
            measurement = prepared[name]
            path = measurement.get("source_path") or prepared.get("config", {}).get(f"{name}_image")
            if path:
                self.image_display_paths[name].set(str(path))
                for canvas in (self.image_canvases[name], self.review_canvases[name]):
                    canvas.set_image(path, reset=False)
                    canvas.set_settings(self.processing[name])
                    canvas.set_calibration(float(self._var(f"{name}_nm_per_pixel").get()), float(self._var(f"{name}_nm_per_pixel_y").get()), source=self.calibration_source[name])
                    canvas.set_overlay(measurement)
        self._suspend = False
        text = self._format_metrics(prepared["sem"]["metrics"], prepared["tem"]["metrics"])
        self._set_text(self.review_summary, text)
        self._set_text(self.measurements, text)
        quality = prepared.get("quality", {})
        warnings = quality.get("warnings", []) if isinstance(quality, dict) else quality
        diagnostics = self._quality_diagnostics(prepared["sem"]["metrics"], prepared["tem"]["metrics"])
        self._write_quality([{"severity": "info", "message": diagnostics}, *warnings])
        self.review_quality_summary.set(f"有 {len(warnings)} 项提示，请查看完整质量信息。" if warnings else "未发现自动检查问题；仍需核对边缘与标尺。")
        self.region_editor.set_domain(prepared["geometry"])
        self._update_resources()

    def _write_quality(self, warnings, append=False):
        self.quality.configure(state="normal")
        if not append:
            self.quality.delete("1.0", "end")
            self.quality.insert("end", "请核对边缘、标尺、截面位置与材料。运行成功不能替代测量验证。\n", "info")
        for warning in warnings or []:
            if isinstance(warning, dict):
                severity = warning.get("severity", warning.get("level", "warning"))
                text = warning.get("message_zh", warning.get("message", warning.get("text", str(warning))))
            else:
                severity, text = "warning", str(warning)
            self.quality.insert("end", "• " + self._translate_warning(text) + "\n", severity if severity in {"warning", "error", "info"} else "warning")
        self.quality.configure(state="disabled")

    @staticmethod
    def _translate_warning(text):
        common = {
            "Voxel discretization error is separate from image measurement error; refine the grid and perform solver convergence tests.": "体素离散误差与图像测量误差不同；定量仿真需细化网格并做收敛检查。",
            "One SEM plan view and one TEM section do not uniquely determine arbitrary 3D morphology; sidewall profiles away from the section are assumed.": "SEM 俯视图与一张 TEM 截面无法唯一确定任意三维结构；截面之外的侧壁由模型延拓。",
            "Material identity and layer positions are user inputs, not inferred from grayscale.": "材料种类和材料层位置由用户指定，不能直接从灰度推断。",
            "Finite observed mark; periodic boundaries require a separately justified repeatable unit cell.": "当前是有限范围的标记；使用周期边界前，需要确定可重复的单元。",
            "Meep narrowband conductivity matches absorption at the stated center wavelength; tabulated n,k are not automatically a causal broadband model.": "Meep 的单波长吸收按指定中心波长匹配；n、k 表格不能直接充当宽带色散模型。",
            "STL contains only grating ridge geometry, in um, and does not include substrate or material regions; use material-ID volume for multimaterial simulation.": "STL 仅包含光栅形貌，单位 µm；多材料仿真使用带材料 ID 的体素数据。",
            "Bright-rim centres are contrast features: inspect peak pairing and validate the physical edge definition": "双亮边配对根据对比度峰定位；请核对峰配对，并验证它是否对应真实物理边界。",
            "SEM has prominent bright bands: threshold edges may follow the inner rim; inspect bright-rim mode and confirm physical boundaries": "SEM 有明显亮带，阈值可能识别到亮带内缘；请比较双亮边模式并核对物理边界。",
            "Long unmeasured SEM stretches are omitted from 3D geometry; confirm whether they are defects or acquisition loss": "SEM 的长缺失段已保留为空缺；请判断是实际缺陷还是成像失效。默认空缺沿完整光栅高度贯通。",
            "All missing SEM rows were explicitly interpolated; genuine missing structure may have been filled": "已按所选选项插值补全所有缺失段；真实结构缺口可能被补上，请核对模型假设。",
            "Interpolated edges are flagged separately from measured edges": "部分边缘通过插值补全，叠加图中以黄色虚线标记，与实测边缘分开显示。",
            "TEM mark tops are user supplied; layers above those markers are excluded from ridge geometry": "采用了人工指定的 TEM 顶面；标记上方的保护层不计入光栅结构。",
            "TEM heights vary significantly across ridges: inspect substrate tilt, top markers and section geometry": "不同光栅高度差异较大；请核对基底倾斜、顶面标记和截面位置。",
            "Automatically found TEM tops may include protective caps; confirm the actual mark interface": "自动找到的 TEM 顶面可能包含保护盖；请核对实际结构界面，必要时人工标记顶面。",
            "SEM and TEM top CDs differ by more than the configured tolerance; verify scales and section correspondence": "SEM 与 TEM 顶部线宽差异超过容差；请核对两个标尺及截面对应关系。",
            "Non-finite native intensity samples are excluded from edge measurement": "图像中有非有限灰度值，相关像素已从边缘测量中排除。",
            "Rotation fill pixels are excluded; coordinates refer to the processed image": "旋转造成的空白填充像素不参与测量；标注坐标对应裁切、旋转后的处理图。",
        }
        text = str(text)
        if text in common:
            return common[text]
        match = re.fullmatch(r"TEM ridge (\d+) sidewall residual exceeds the review limit; inspect the chosen (\w+) model", text)
        if match:
            return f"第 {int(match[1]) + 1} 条 TEM 光栅的侧壁拟合残差较大；请核对轮廓并比较直线与曲线模型。"
        match = re.fullmatch(r"TEM ridge (\d+) has unmeasured sidewall samples; possible internal interfaces or image corruption", text)
        if match:
            return f"第 {int(match[1]) + 1} 条 TEM 光栅有未测到的侧壁点；请核对内部材料界面或图像损坏。"
        match = re.fullmatch(r"Region (.+) has no final voxels: outside domain, sub-voxel thickness, or overwritten by higher priority\.", text)
        if match:
            return f"材料区域 {match[1]} 的最终体素数为 0；可能在模型范围外、薄于体素间距，或被其他区域覆盖。"
        return text

    @staticmethod
    def _quality_diagnostics(sem, tem):
        parts = []
        for key, label in (("edge_valid_fraction", "SEM 实测边缘"), ("edge_interpolated_fraction", "插值边缘"), ("ridge_absent_fraction", "保留缺失")):
            values = sem.get(key)
            if values is not None and len(values):
                parts.append(f"{label} {100 * sum(values) / len(values):.1f}%")
        residual = tem.get("sidewall_fit_rms_mean_nm")
        if residual is not None:
            parts.append(f"TEM 侧壁拟合 RMS {float(residual):.1f} nm")
        return "；".join(parts) or "请核对边缘叠加图。"

    def _update_resources(self):
        if not self.prepared or not hasattr(self, "resource_summary"):
            return
        try:
            from grating3d.pipeline import estimate_grid
            estimate = estimate_grid(self.prepared["geometry"], self._collect_config(for_export=False, for_save=True))
            count = estimate.get("voxel_count", estimate.get("count", 0))
            memory = estimate.get("estimated_memory_mb", estimate.get("estimated_peak_mb"))
            self.resource_summary.set(f"网格 {estimate.get('shape_xyz', '')}，约 {count:,} 个体素" + (f"；估计占用 {memory:.0f} MB" if memory is not None else "") + "。不包括电磁求解内存。")
        except Exception as exc:
            self.resource_summary.set(f"网格估算：{exc}")

    def _show_result(self, result):
        report = result["report"]
        self.result_state.set(f"结果对应已保存运行：{Path(result['output_dir']).name}。预览不会随输入参数自动重算。")
        text = self._format_metrics(report["measurements"]["sem"], report["measurements"]["tem"])
        text += f"\n导出波长 {report['wavelength_nm']:g} nm；体素 xyz {report['grid']['shape_xyz']}；坐标 nm / STL µm"
        self._set_text(self.measurements, text)
        self._write_quality([{"severity": "info", "message": self._quality_diagnostics(report["measurements"]["sem"], report["measurements"]["tem"])}, *report.get("warnings", [])])
        region_reports = report.get("regions", [])
        ordered_regions = sorted(region_reports, key=lambda region: region.get("region_id", 0))
        recorded = [{key: value for key, value in region.items() if key not in {"region_id", "selected_voxels", "final_voxels"}} for region in ordered_regions]
        try:
            current = self.region_editor.get_regions()
        except ValueError:
            current = None
        self.region_editor.set_coverage(region_reports if recorded == current else [])
        self.preview_paths = {key: result["files"].get(key) for key in self.preview_labels}
        self._resize_previews()
        self._append_log("结果文件已生成；请检查质量提示及材料切片。")

    def _schedule_preview_resize(self, _event=None):
        if self._preview_after:
            self.after_cancel(self._preview_after)
        self._preview_after = self.after(150, self._resize_previews)

    def _resize_previews(self):
        self._preview_after = None
        from PIL import Image, ImageTk
        for key, label in self.preview_labels.items():
            path = self.preview_paths.get(key)
            if not path:
                continue
            try:
                with Image.open(path) as original:
                    picture = original.convert("RGB")
                    picture.thumbnail((max(200, label.winfo_width() - 8), max(150, label.winfo_height() - 8)), Image.Resampling.LANCZOS)
                self.preview_images[key] = ImageTk.PhotoImage(picture, master=self.master)
                label.configure(image=self.preview_images[key], text="")
            except Exception as exc:
                label.configure(image="", text=f"预览无法加载：{exc}")

    def _select_history(self, _event=None):
        index = self.history_combo.current()
        if 0 <= index < len(self.history):
            self.result = self.history[index]
            self._show_result(self.result)
            self.status.set(f"正在查看历史结果：{Path(self.result['output_dir']).name}")

    @staticmethod
    def _open_local(path):
        path = Path(path).resolve()
        if not path.exists():
            raise ValueError(f"路径不存在：{path}")
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def open_output(self):
        try:
            self._open_local(self.result["output_dir"] if self.result else self._var("output_dir").get())
        except Exception as exc:
            messagebox.showerror("目录无法打开", str(exc))

    def open_interactive(self):
        if self.result and self.result["files"].get("interactive_preview"):
            webbrowser.open(Path(self.result["files"]["interactive_preview"]).resolve().as_uri())

    def open_report(self):
        if self.result:
            self._open_local(Path(self.result["output_dir"]) / "report.json")

    def destroy(self):
        # Callbacks belong to this widget: cancel them here before destroying
        # the Tcl commands, allowing annotation windows to close independently.
        for name in ("_poll_after", "_preview_after"):
            callback = getattr(self, name, None)
            if callback:
                self.after_cancel(callback)
                setattr(self, name, None)
        super().destroy()

    def close(self):
        if self.busy:
            self._close_requested = True
            self.cancel()
            self.status.set("已请求取消；等待当前阶段安全结束后自动关闭窗口。")
            return
        self.master.destroy()


def main():
    root = tk.Tk()
    root.title("SEM / TEM 光栅三维重建 · 几何与材料导出")
    root.geometry("1200x820")
    root.minsize(950, 650)
    try:
        ttk.Style(root).theme_use("clam")
    except tk.TclError:
        pass
    ReconstructionApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
