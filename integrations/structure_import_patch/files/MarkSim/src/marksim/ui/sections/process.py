"""GDS/process editor; all geometry calculations remain in the service layer."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk
from uuid import uuid4
import numpy as np

from ..widgets import ImageView, JsonEditor
from ..controls import Card, Collapsible, Tooltip
from ..theme import COLORS, FONTS


def step_height_summary(step):
    """Describe the declared interval without modifying or completing the recipe."""
    def number(value):
        if value is None:
            return "未填写"
        try:
            return f"{float(value):g}" if np.isfinite(float(value)) else "无效"
        except (TypeError, ValueError):
            return "无效"

    if step.get("operation") == "etch":
        return f"参考面 {number(step.get('start_z_um'))}；深度 {number(step.get('etch_depth_um'))}"
    if step.get("operation") == "create_film":
        mode = step.get("film_mode", "absolute")
        if mode == "conformal":
            return f"包覆：顶 {number(step.get('thickness_um'))} / 侧 {number(step.get('sidewall_thickness_um'))}"
        if mode == "planar_fill":
            return f"填平：最高面 + {number(step.get('thickness_um'))}"
    bottom = step.get("z_min_um")
    top = step.get("z_max_um")
    if top is None and bottom is not None and step.get("thickness_um") is not None:
        try:
            top = float(bottom) + float(step["thickness_um"])
        except (TypeError, ValueError):
            return "高度输入无效"
    return f"{number(bottom)} → {number(top)}"


class ProcessSection:
    """Feature methods sharing the single MarkSimApp state owner."""

    def _process_page(self):
        page = self.pages[1]
        self.operation_labels = {"创建衬底": "create_substrate", "表面薄膜": "create_film", "按掩模添加": "add", "垂直几何刻蚀": "etch", "删除指定材料": "delete"}
        self.film_mode_labels = {
            "等厚包覆（贴合表面）": "conformal",
            "填平覆盖（平坦顶面）": "planar_fill",
            "指定高度（旧模型）": "absolute",
        }
        summary = Card(page, padding=14)
        summary.pack(fill="x", pady=(0, 14))
        self.process_dimensions = {}
        for i, (key, title) in enumerate((("mark_type", "Mark 类型"), ("mother", "母周期 · µm"),
                                         ("subpitch", "子周期 · µm"), ("cd", "模板 CD · nm"),
                                         ("height", "模板高度 · nm"))):
            cell = ttk.Frame(summary.body, style="Surface.TFrame", padding=(12 if i else 0, 0))
            cell.grid(row=0, column=i, sticky="nsew")
            summary.body.columnconfigure(i, weight=1, uniform="mark_dimensions")
            ttk.Label(cell, text=title, style="Surface.TLabel", foreground=COLORS["muted"], font=FONTS["small"]).pack(anchor="w")
            self.process_dimensions[key] = tk.StringVar(value="—")
            ttk.Label(cell, textvariable=self.process_dimensions[key], style="Surface.TLabel",
                      font=(FONTS["body"][0], -24)).pack(anchor="w", pady=(4, 0))
        row = ttk.Frame(page, style="App.TFrame")
        row.pack(fill="x", pady=(0, 8))
        self.button(row, "Mark 类型与尺寸…", self.edit_mark)
        self.button(row, "母周期与计算单元…", lambda: self.edit_json("process"))
        self.button(row, "检查 GDS 图层…", self.inspect_gds)
        self.mark_description = tk.StringVar(value="未打开工程 · 可新建 AH32 / AH53 / AH74 参数化 Mark。")
        mark_note = ttk.Label(page, textvariable=self.mark_description, style="Muted.TLabel", justify="left")
        mark_note.pack(anchor="w", fill="x", pady=(0, 14))
        page.bind("<Configure>", lambda event: mark_note.configure(wraplength=max(240, event.width-24)), add="+")

        self.process_workspace = ttk.Frame(page, style="App.TFrame")
        self.process_workspace.pack(fill="x")
        self.process_editor_column = ttk.Frame(self.process_workspace, style="App.TFrame")
        self.process_preview_column = ttk.Frame(self.process_workspace, style="App.TFrame")
        # Measure the visible canvas, not the body's requested width: a wide
        # child must not lock the breakpoint into a horizontal-scroll layout.
        self.page_scrollers[1].canvas.bind("<Configure>", self._layout_process_columns, add="+")
        self._process_wide = None
        self._layout_process_columns()
        list_card = Card(self.process_editor_column, "工艺步骤", padding=12)
        list_card.pack(fill="x")
        row = ttk.Frame(list_card.body)
        row.pack(fill="x", pady=(0, 7))
        for label, command in (("＋ 新增", self.add_step), ("删除", self.delete_step),
                               ("上移", lambda: self.move_step(-1)), ("下移", lambda: self.move_step(1))):
            ttk.Button(row, text=label, style="Compact.TButton", command=lambda fn=command: self.safe(fn)).pack(side="left", padx=(0, 5))
        table = ttk.Frame(list_card.body)
        table.pack(fill="x")
        self.process_tree = ttk.Treeview(table, columns=("op", "mat", "z", "on"), displaycolumns=("on",),
                                        show="tree headings", height=3, selectmode="browse")
        for key, title, width in (("#0", "按顺序执行 · 选择步骤编辑", 250), ("op", "几何操作", 115), ("mat", "材料 / 目标", 150), ("z", "z 范围 / 深度 (µm)", 180), ("on", "状态", 55)):
            self.process_tree.heading(key, text=title)
            self.process_tree.column(key, width=width, minwidth=55, stretch=key != "on", anchor="center" if key == "on" else "w")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.process_tree.yview)
        self.process_tree.configure(yscrollcommand=scrollbar.set)
        self.process_tree.pack(side="left", fill="x", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.process_tree.bind("<<TreeviewSelect>>", self.select_step)
        ttk.Separator(list_card.body).pack(fill="x", pady=(12, 10))
        self.step_editor_title = tk.StringVar(value="编辑选中步骤")
        title_row = ttk.Frame(list_card.body)
        title_row.pack(fill="x", pady=(0, 10))
        ttk.Label(title_row, textvariable=self.step_editor_title, style="Surface.TLabel", font=FONTS["strong"]).pack(side="left")
        self.step_enabled = tk.BooleanVar(value=True)
        self.step_enabled_check = ttk.Checkbutton(title_row, text="启用", variable=self.step_enabled)
        self.step_enabled_check.pack(side="right")
        form = ttk.Frame(list_card.body)
        form.pack(fill="x")
        self.step_form = form
        self.step_vars = {name: tk.StringVar() for name in ("operation", "material", "film_mode", "z", "height", "sidewall", "depth", "cd", "dx")}
        self.step_entries = {}
        self.step_field_labels = {}
        self.step_field_cells = {}
        specs = [("operation", "几何操作"), ("material", "材料 / 目标材料"), ("film_mode", "薄膜形貌"),
                 ("z", "z 起点 / 参考面 · nm"), ("height", "厚度 / 删除高度 · nm"), ("sidewall", "侧壁厚度 · nm"),
                 ("depth", "刻蚀深度 · nm"), ("cd", "CD 偏置 · nm"), ("dx", "层间 Δx · nm")]
        for key, label in specs:
            cell = ttk.Frame(form)
            self.step_field_cells[key] = cell
            self.step_field_labels[key] = ttk.Label(cell, text=label, wraplength=160)
            self.step_field_labels[key].pack(anchor="w", pady=(0, 4))
            if key == "operation":
                entry = ttk.Combobox(cell, textvariable=self.step_vars[key], values=list(self.operation_labels), state="readonly", width=12)
            elif key == "film_mode":
                entry = ttk.Combobox(cell, textvariable=self.step_vars[key], values=list(self.film_mode_labels), state="readonly", width=12)
            elif key == "material":
                entry = self.step_material_combo = ttk.Combobox(cell, textvariable=self.step_vars[key], width=12)
                Tooltip(entry, "添加操作选择一种材料。刻蚀或删除多个目标材料时，使用英文逗号分隔名称。")
            else:
                entry = ttk.Entry(cell, textvariable=self.step_vars[key], width=12)
            self.step_entries[key] = entry
            entry.pack(fill="x")
            cell.bind("<Configure>", lambda event, label=self.step_field_labels[key]: label.configure(wraplength=max(80, min(160, event.width))))
        for column in range(2):
            form.columnconfigure(column, weight=1, uniform="process_fields")
        controls = ttk.Frame(list_card.body)
        controls.pack(fill="x", pady=(0, 10))
        ttk.Label(controls, text="长度输入 nm · 侧壁 90°", style="Surface.TLabel",
                  foreground=COLORS["muted"], font=FONTS["small"]).pack(side="left")
        self.step_apply_button = ttk.Button(controls, text="应用表单 →", style="Primary.TButton", command=lambda: self.safe(self.apply_step_form))
        self.step_apply_button.pack(side="right")
        self.step_form_note = tk.StringVar(value="选中工艺步骤后编辑，再点击“应用表单”。")
        note = ttk.Label(list_card.body, textvariable=self.step_form_note, style="Surface.TLabel",
                         foreground=COLORS["muted"], font=FONTS["small"], justify="left", wraplength=340)
        note.pack(anchor="w", fill="x", pady=(0, 8))
        list_card.body.bind("<Configure>", lambda event: note.configure(wraplength=max(200, min(340, event.width))), add="+")

        preview_card = Card(self.process_preview_column, "结构预览", padding=12)
        preview_card.pack(fill="x")
        self.process_geometry_status = tk.StringVar(value="尚未生成几何 · 生成后在此显示真实结构。")
        status = ttk.Label(preview_card.body, textvariable=self.process_geometry_status, style="Surface.TLabel",
                           foreground=COLORS["muted"], font=FONTS["small"], justify="left", wraplength=480)
        status.pack(fill="x", pady=(0, 6))
        preview_card.body.bind("<Configure>", lambda event: status.configure(wraplength=max(200, event.width)), add="+")
        self.process_geometry_image = ImageView(preview_card.body)
        self.process_geometry_image.configure(width=1, height=315)
        self.process_geometry_image.pack(fill="x")
        preview_actions = ttk.Frame(preview_card.body)
        preview_actions.pack(fill="x", pady=(8, 0))
        ttk.Button(preview_actions, text="原始截面图 ↗", style="Compact.TButton", command=lambda: self.safe(self.open_geometry_sections)).pack(side="left", padx=(0, 8))
        ttk.Button(preview_actions, text="旋转 3D 视图 ↗", style="Compact.TButton", command=lambda: self.safe(self.open_geometry_3d)).pack(side="left")
        ttk.Button(preview_actions, text="几何详情 →", style="Compact.TButton", command=lambda: self.tabs.select(2)).pack(side="right")
        geometry_card = Card(self.process_preview_column, "几何摘要", "已显示几何中的工艺对象；分片不等于新增膜层。", padding=12)
        geometry_card.pack(fill="x", pady=(14, 0))
        geometry_table = ttk.Frame(geometry_card.body)
        geometry_table.pack(fill="x")
        self.process_geometry_tree = ttk.Treeview(geometry_table, columns=("z", "material", "status"), show="tree headings", height=3)
        for key, title, width in (("#0", "工艺对象", 145), ("z", "z 范围 · µm", 105), ("material", "材料", 135), ("status", "状态", 90)):
            self.process_geometry_tree.heading(key, text=title)
            self.process_geometry_tree.column(key, width=width, minwidth=65, stretch=True)
        geometry_ybar = ttk.Scrollbar(geometry_table, command=self.process_geometry_tree.yview)
        self.process_geometry_tree.configure(yscrollcommand=geometry_ybar.set)
        self.process_geometry_tree.pack(side="left", fill="x", expand=True)
        geometry_ybar.pack(side="right", fill="y")
        self.process_geometry_source = tk.StringVar(value="图像与摘要来自同一几何快照。")
        source = ttk.Label(geometry_card.body, textvariable=self.process_geometry_source, style="Surface.TLabel",
                           foreground=COLORS["muted"], font=FONTS["small"], justify="left", wraplength=480)
        source.pack(fill="x", pady=(8, 0))
        geometry_card.body.bind("<Configure>", lambda event: source.configure(wraplength=max(200, event.width)), add="+")

        self.step_advanced = Collapsible(page, "高级：GDS 掩模与步骤 JSON")
        self.step_advanced.pack(fill="x", pady=(14, 8))
        editor = self.step_advanced.body
        ttk.Label(editor, text="cell、layer、datatype、ROI 和正反掩模在此编辑；JSON 修改需单独应用。", style="Muted.TLabel", wraplength=740).pack(anchor="w", pady=(0, 6))
        text_frame = ttk.Frame(editor)
        text_frame.pack(fill="both", expand=True)
        self.step_text = tk.Text(text_frame, height=10, width=1, font=FONTS["mono"], wrap="none", undo=True)
        yscroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.step_text.yview)
        xscroll = ttk.Scrollbar(text_frame, orient="horizontal", command=self.step_text.xview)
        self.step_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.step_text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        bar = ttk.Frame(editor)
        bar.pack(fill="x", pady=(6, 0))
        self.button(bar, "应用 JSON 修改", self.apply_step)
        self.button(bar, "选择此步骤 GDS…", self.choose_step_gds)
        self.step_index = None
        # Mode traces also run during draft restore, so disabled fields and the
        # explanation always match the restored, not-yet-applied form.
        for key in ("operation", "film_mode"):
            self.step_vars[key].trace_add("write", lambda *_args: self._update_step_controls())
        self._update_step_controls()

    def _layout_process_columns(self, event=None):
        """Keep the editor beside the real preview when the viewport permits."""
        width = event.width if event is not None else self.page_scrollers[1].canvas.winfo_width()
        wide = width >= 960
        if self._process_wide == wide:
            return
        self._process_wide = wide
        for column in (self.process_editor_column, self.process_preview_column):
            column.grid_forget()
        # The first grid track includes the 14 px gutter. Labels and field
        # requests are bounded so the actual editor stays at 370 px when wide.
        self.process_workspace.columnconfigure(0, weight=0 if wide else 1, minsize=384 if wide else 0)
        self.process_workspace.columnconfigure(1, weight=1 if wide else 0, minsize=0)
        self.process_editor_column.grid(row=0, column=0, sticky="new", padx=(0, 14 if wide else 0))
        self.process_preview_column.grid(row=0 if wide else 1, column=1 if wide else 0, sticky="new", pady=(0 if wide else 14, 0))

    def _refresh_process_dimensions(self):
        """Show applied template metadata, never infer a type from a GDS name."""
        for variable in self.process_dimensions.values():
            variable.set("—")
        config = self.data["process"].get("mark") if self.data else None
        if not config:
            self.process_dimensions["mark_type"].set("自定义 GDS" if self.data else "—")
            period = self.data["process"].get("period_x_um") if self.data else None
            try:
                if period is not None and np.isfinite(float(period)):
                    self.process_dimensions["mother"].set(f"{float(period):g}")
            except (TypeError, ValueError, OverflowError):
                pass
            return
        from ...marks import normalize_mark_config
        try:
            mark = normalize_mark_config(config)
        except (ValueError, NotImplementedError, KeyError, TypeError):
            self.process_dimensions["mark_type"].set("待修正")
            return
        values = {"mark_type": mark["mark_type"], "mother": f"{mark['mother_period_um']:g}",
                  "subpitch": f"{mark['subpitch_um']:g}", "cd": f"{1000*mark['cd_um']:g}",
                  "height": f"{1000*mark['height_um']:g}"}
        for key, value in values.items():
            self.process_dimensions[key].set(value)

    def update_process_geometry(self, preview_folder, geometry):
        """Mirror the same saved geometry shown by the full geometry page."""
        from ...geometry_display import step_groups
        image = Path(preview_folder) / "geometry_sections.png"
        self.process_geometry_image.clear()
        if image.is_file():
            self.process_geometry_image.show(image)
        self.process_geometry_tree.delete(*self.process_geometry_tree.get_children())
        groups = step_groups(geometry)
        self.process_geometry_tree.configure(height=max(2, min(5, len(groups))))
        for i, group in enumerate(groups):
            interval = "—" if group["z_min_um"] is None else f"{group['z_min_um']:g} → {group['z_max_um']:g}"
            self.process_geometry_tree.insert("", "end", iid=str(i), text=group["label"],
                                              values=(interval, group["material"] or "—", group["status"]))
        self.process_geometry_status.set("已保存几何 · 表单输入不会自动更新此图。")
        self.process_geometry_source.set(f"母周期 {geometry.get('period_x_um', '—')} µm · {len(groups)} 个工艺对象 · {len(geometry.get('solids', []))} 个计算分片")

    def clear_process_geometry(self):
        self.process_geometry_image.clear()
        self.process_geometry_tree.delete(*self.process_geometry_tree.get_children())
        self.process_geometry_status.set("尚未生成几何 · 生成后在此显示真实结构。")
        self.process_geometry_source.set("图像与摘要来自同一几何快照。")

    def invalidate_process_geometry(self):
        if self.process_geometry_image.path is not None:
            self.process_geometry_status.set("物理输入已修改 · 此图为上次几何，请重新生成。")

    def _update_step_controls(self):
        """Expose only values the selected geometric rule actually consumes."""
        op = self.operation_labels.get(self.step_vars["operation"].get())
        mode = self.film_mode_labels.get(self.step_vars["film_mode"].get(), "absolute")
        selected = getattr(self, "step_index", None) is not None
        film = op == "create_film"
        surface = film and mode in ("conformal", "planar_fill")
        enabled = {
            "film_mode": film, "z": bool(op) and not surface,
            "height": bool(op) and op != "etch",
            "sidewall": film and mode == "conformal", "depth": op == "etch",
            "cd": bool(op) and not surface, "dx": bool(op) and not surface,
        }
        for key, active in enabled.items():
            self.step_entries[key].configure(state=("readonly" if key == "film_mode" else "normal") if active and selected else "disabled")
        self.step_entries["operation"].configure(state="readonly" if selected else "disabled")
        self.step_entries["material"].configure(state="normal" if selected else "disabled")
        self.step_enabled_check.configure(state="normal" if selected else "disabled")
        self.step_apply_button.configure(state="normal" if selected else "disabled")
        # Keep each mode compact while retaining the original variables and
        # widgets, including uncommitted values restored from a form draft.
        fields = [("operation", "material")]
        if film:
            fields.append(("film_mode",))
        fields.append(("z", "depth" if op == "etch" else "height"))
        if film and mode == "conformal":
            fields.append(("sidewall",))
        fields.append(("cd", "dx"))
        for cell in self.step_field_cells.values():
            cell.grid_remove()
        for row, keys in enumerate(fields):
            for column, key in enumerate(keys):
                self.step_field_cells[key].grid(row=row, column=column, columnspan=2 if key == "film_mode" else 1,
                                                sticky="ew", padx=(0, 10 if column == 0 and key != "film_mode" else 0), pady=(0, 10))
        height_label = "顶面 / 槽底厚度 · nm" if surface and mode == "conformal" else "高出最高表面 · nm" if surface else "厚度 / 删除高度 · nm"
        self.step_field_labels["height"].configure(text=height_label)
        if surface and mode == "conformal":
            note = "等厚包覆：顶面和沟槽底按所填厚度贴合；侧壁厚度独立填写，可为 0。覆盖全部已有表面；应用时移除旧掩模、CD 偏置和位移。"
        elif surface:
            note = "填平覆盖：从已有表面填满凹槽，顶面统一为“当前最高表面 + 厚度”，不留悬空层。覆盖全单元；应用时移除旧掩模和变换。"
        elif film:
            note = "指定高度（旧模型）：按 z 起点和厚度放置整层，可覆盖原材料或产生悬空；打开旧工程不自动改形状。要贴合下层，请切换上方薄膜形貌。"
        else:
            note = "上表面由“z 起点 + 厚度”确定；刻蚀使用参考面与深度。高级 JSON 支持完整掩模属性。"
        self.step_form_note.set(note if op and selected else "选中工艺步骤后编辑，再点击“应用表单”。")

    def refresh_steps(self):
        from ..mark_editor import refresh_mark_labels
        refresh_mark_labels(self)
        self._refresh_process_dimensions()
        self.process_tree.delete(*self.process_tree.get_children()); self.step_index = None
        self.step_editor_title.set("编辑选中步骤")
        self._update_step_controls()
        if not self.data: return
        self.process_tree.configure(height=max(2, min(5, len(self.data["process"]["steps"]))))
        operation_names = {value: key for key, value in self.operation_labels.items()}
        for i, step in enumerate(self.data["process"]["steps"]):
            material = step.get("material", ", ".join(step.get("target_materials", [])))
            self.process_tree.insert("", "end", iid=str(i), text=f"{i+1:02d}  {step.get('name', step.get('id', str(i)))}", values=(operation_names.get(step["operation"], step["operation"]), material,
                step_height_summary(step), "启用" if step.get("enabled", True) else "停用"))

    def edit_mark(self):
        self._assert_recipe_editable()
        from ..mark_editor import show_mark_editor
        return show_mark_editor(self)

    def select_step(self, event=None):
        selection = self.process_tree.selection()
        if not selection: return
        if event is not None and self.step_index == int(selection[0]): return
        self.step_index = int(selection[0]); self.step_text.delete("1.0", "end")
        self.step_editor_title.set(f"编辑步骤 {self.step_index+1:02d}")
        self.step_text.insert("1.0", json.dumps(self.data["process"]["steps"][self.step_index], indent=2, ensure_ascii=False))
        step = self.data["process"]["steps"][self.step_index]
        self.step_vars["operation"].set(next((label for label, op in self.operation_labels.items() if op == step["operation"]), step["operation"]))
        self.step_vars["material"].set(step.get("material", ",".join(step.get("target_materials", []))))
        mode = step.get("film_mode", "absolute") if step["operation"] == "create_film" else "conformal"
        self.step_vars["film_mode"].set(next((label for label, value in self.film_mode_labels.items() if value == mode), mode))
        self.step_vars["z"].set(f"{1000*step.get('start_z_um', step.get('z_min_um', 0)):g}")
        height = step["z_max_um"]-step.get("z_min_um", 0) if "z_max_um" in step else step.get("thickness_um", .1)
        for key, value in (("height", height), ("depth", step.get("etch_depth_um", .1)), ("cd", step.get("cd_bias_um", 0)), ("dx", step.get("delta_x_um", 0))): self.step_vars[key].set(f"{1000*value:g}")
        # A malformed conformal recipe must visibly request its missing side
        # thickness; opening the editor must not silently complete the recipe.
        sidewall = step.get("sidewall_thickness_um", None if mode == "conformal" and step["operation"] == "create_film" else .1)
        self.step_vars["sidewall"].set("" if sidewall is None else f"{1000*sidewall:g}")
        self.step_enabled.set(step.get("enabled", True))
        self._update_step_controls()

    def apply_step_form(self):
        self._assert_recipe_editable()
        if self.step_index is None: raise ValueError("先选中工艺步骤")
        index = self.step_index; step = copy.deepcopy(self.data["process"]["steps"][index])
        op = self.operation_labels[self.step_vars["operation"].get()]
        mode = self.film_mode_labels.get(self.step_vars["film_mode"].get(), "")
        if op == "create_film" and not mode:
            raise ValueError("请选择薄膜形貌")
        surface = op == "create_film" and mode != "absolute"

        def length(key, *, positive=False, nonnegative=False):
            label = self.step_field_labels[key].cget("text")
            try:
                value = float(self.step_vars[key].get()) / 1000
            except (TypeError, ValueError):
                raise ValueError(f"{label}：请填写有限数字") from None
            if not np.isfinite(value) or (positive and value <= 0) or (nonnegative and value < 0):
                raise ValueError(f"{label}：需要{'正值' if positive else '非负值' if nonnegative else '有限数字'}")
            return value

        height = length("height", positive=True) if op != "etch" else None
        z = length("z") if not surface else None
        depth = length("depth", positive=True) if op == "etch" else None
        cd, dx = (length("cd"), length("dx")) if not surface else (0, 0)
        sidewall = length("sidewall", nonnegative=True) if surface and mode == "conformal" else None
        material = self.step_vars["material"].get().strip()
        if not material: raise ValueError("选择材料 / 目标材料")
        if surface and material == self.data["process"].get("background_material"):
            raise ValueError("表面薄膜请选择实际材料；背景材料已填充空白区域")
        step.update(operation=op, enabled=self.step_enabled.get(), cd_bias_um=cd, delta_x_um=dx, sidewall_angle_deg=90)
        for key in ("material", "target_materials", "z_min_um", "z_max_um", "thickness_um", "start_z_um", "etch_depth_um", "film_mode", "sidewall_thickness_um"): step.pop(key, None)
        if op == "etch": step.update(target_materials=[x.strip() for x in material.split(",")], start_z_um=z, etch_depth_um=depth)
        elif op == "delete": step.update(target_materials=[x.strip() for x in material.split(",")], z_min_um=z, z_max_um=z+height)
        elif surface:
            # Surface deposition is explicitly a whole-cell operation. Removing
            # stale mask/transforms prevents silently ignoring hidden JSON.
            for key in ("mask", "mask_polarity", "cd_bias_um", "delta_x_um", "delta_y_um", "rotation_deg"):
                step.pop(key, None)
            step.update(material=material, film_mode=mode, thickness_um=height)
            if mode == "conformal":
                step["sidewall_thickness_um"] = sidewall
        else:
            step.update(material=material, z_min_um=z, thickness_um=height)
            if op == "create_film":
                step["film_mode"] = "absolute"
        self.data["process"]["steps"][index] = step; self.refresh_steps(); self.process_tree.selection_set(str(index)); self.select_step(); self.mark_changed("physics")

    def apply_step(self):
        self._assert_recipe_editable()
        if self.step_index is None: raise ValueError("先选中一个工艺步骤")
        value = json.loads(self.step_text.get("1.0", "end"))
        if not isinstance(value, dict) or "operation" not in value: raise ValueError("步骤需要 operation 字段")
        self.data["process"]["steps"][self.step_index] = value
        self.refresh_steps(); self.mark_changed("physics")

    def add_step(self):
        self._assert_recipe_editable()
        if not self.data: raise ValueError("先打开工程")
        steps = self.data["process"]["steps"]
        background = self.data["process"].get("background_material")
        materials = self.data["materials"]["materials"]
        candidates = [s.get("material") for s in reversed(steps)] + list(materials)
        material = next((name for name in candidates if name in materials and name != background and "AIR" not in name.upper()), "")
        step_id = f"film_{uuid4().hex[:12]}"
        while any(s.get("id") == step_id for s in steps):
            step_id = f"film_{uuid4().hex[:12]}"
        steps.append({"id": step_id, "name": "新薄膜", "operation": "create_film", "material": material,
                      "film_mode": "conformal", "thickness_um": .1, "sidewall_thickness_um": .1,
                      "enabled": True, "sidewall_angle_deg": 90})
        self.refresh_steps(); self.process_tree.selection_set(str(len(steps)-1)); self.select_step(); self.mark_changed("physics")

    def delete_step(self):
        self._assert_recipe_editable()
        if self.step_index is None: raise ValueError("先选中步骤")
        del self.data["process"]["steps"][self.step_index]; self.refresh_steps(); self.mark_changed("physics")

    def move_step(self, delta):
        self._assert_recipe_editable()
        if self.step_index is None: raise ValueError("先选中步骤")
        i = self.step_index; j = i+delta; steps = self.data["process"]["steps"]
        if 0 <= j < len(steps):
            steps[i], steps[j] = steps[j], steps[i]; self.refresh_steps(); self.process_tree.selection_set(str(j)); self.select_step(); self.mark_changed("physics")

    def toggle_step(self):
        self._assert_recipe_editable()
        if self.step_index is None: raise ValueError("先选中步骤")
        step = self.data["process"]["steps"][self.step_index]; step["enabled"] = not step.get("enabled", True)
        self.refresh_steps(); self.mark_changed("physics")

    def choose_step_gds(self):
        self._assert_recipe_editable()
        if self.step_index is None: raise ValueError("先选中掩模步骤")
        path = filedialog.askopenfilename(filetypes=[("GDS", "*.gds *.gdsii")])
        if not path: return
        value = json.loads(self.step_text.get("1.0", "end")); value.setdefault("mask", {})["gds_path"] = path
        self.step_text.delete("1.0", "end"); self.step_text.insert("1.0", json.dumps(value, indent=2, ensure_ascii=False))
        self.status.set("已选择原始 GDS；请填写 cell/layer/datatype 并采用步骤修改。")

    def inspect_gds(self):
        from ...geometry import inspect_gds
        path = filedialog.askopenfilename(filetypes=[("GDS", "*.gds *.gdsii")])
        if path: JsonEditor(self, "GDS 实际单位 / cell / 层（查看）", inspect_gds(path), lambda _value: None)
