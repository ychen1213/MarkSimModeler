"""Small transactional Mark editor; geometry and profile policy live in marks.py.

Text in this window is a draft. Only ``apply`` may replace the application's
project dictionaries, so a cancelled dialog cannot change the active physics.
Lengths shown in nm are converted to the core's explicit micrometre units.
"""
from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from .. import marks, service
from .controls import Card, ScrollFrame
from .theme import COLORS


def mark_summary(data):
    """Describe applied metadata only; a GDS filename never proves a Mark type."""
    if not data:
        return "未打开工程 · 可新建 AH32 / AH53 / AH74 参数化 Mark。"
    config = data["process"].get("mark")
    if not config:
        return "自定义 GDS · 尚未分类；可选择目标掩模步骤并设置 Mark 类型。"
    try:
        canonical = marks.normalize_mark_config(config)
    except (ValueError, NotImplementedError, KeyError, TypeError) as exc:
        return f"Mark 配置待修正：{exc}"
    return (f"{canonical['mark_type']}  ·  母周期 {canonical['mother_period_um']:g} µm"
            f"  ·  子 pitch {canonical['subpitch_um']:g} µm"
            f"  ·  模板 CD {1000*canonical['cd_um']:g} nm（工艺偏置另计）"
            f"  ·  {canonical['stripe_count']} 条纹 / 母周期")


def refresh_mark_labels(app):
    """Refresh labels after loading, restoring or deliberately applying a recipe."""
    if hasattr(app, "mark_description"):
        app.mark_description.set(mark_summary(app.data))


class MarkEditor(tk.Toplevel):
    """Create a new parameterized project or replace one selected mask step."""

    def __init__(self, app, *, create=False):
        if app.busy:
            raise ValueError("任务正在运行，请完成或取消后设置 Mark")
        if not create and app.data is None:
            raise ValueError("请先打开工程，或新建 Mark 工程")
        super().__init__(app.root)
        self.app, self.create = app, create
        self.source_folder = app.data["folder"] if app.data and not create else None
        self.title("新建 Mark 工程" if create else "Mark 类型与尺寸")
        self.geometry("760x800"); self.minsize(700, 700)
        self.transient(app.root)
        self._loading = True
        self._drafts = {}
        self._active_type = None
        self.catalog = {entry["id"]: entry for entry in marks.catalog()}
        self.type_var = tk.StringVar()
        self.pitch_mode = tk.StringVar(value="mother")
        self.values = {key: tk.StringVar() for key in ("mother", "subpitch", "cd", "height")}
        self.preview_note = tk.StringVar()
        self.error = tk.StringVar()
        self.target_var = tk.StringVar()
        self.targets = {}

        # Reserve actions before the scrollable form so high-DPI / small windows
        # never hide Apply and Cancel beneath the parameter preview.
        footer = ttk.Frame(self, padding=(22, 8, 22, 16)); footer.pack(side="bottom", fill="x")
        scroller = ScrollFrame(self, padding=22); scroller.pack(fill="both", expand=True)
        body = scroller.body
        ttk.Label(body, text="选择结构，再填写尺寸", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(body, text="AH32 / AH53 / AH74 可计算；其他类型作为明确的预留入口。", style="Muted.TLabel").pack(anchor="w", pady=(5, 15))
        row = ttk.Frame(body); row.pack(fill="x")
        ttk.Label(row, text="Mark 类型").pack(side="left", padx=(0, 18))
        self.type_combo = ttk.Combobox(row, textvariable=self.type_var, values=[entry["label"] for entry in self.catalog.values()], state="readonly", width=24)
        self.type_combo.pack(side="left", fill="x", expand=True)
        self.type_combo.bind("<<ComboboxSelected>>", self.change_type)
        ttk.Button(row, text="恢复此类默认值", command=self.reset_defaults).pack(side="left", padx=(12, 0))

        sizes = Card(body, "模板尺寸", "母周期 L 是 FDTD 周期；子 pitch = L / 目标阶次。模板 CD 为单条纹宽度，后续工艺偏置另计。", padding=12)
        sizes.pack(fill="x", pady=(15, 10))
        self.entries = {}
        for index, (key, label) in enumerate((("mother", "母周期 L · µm"), ("subpitch", "子 pitch · µm"), ("cd", "条纹 CD · nm"), ("height", "添加高度 / 刻蚀深度 · nm"))):
            if key in ("mother", "subpitch"):
                ttk.Radiobutton(sizes.body, text=label, variable=self.pitch_mode, value=key, command=self.change_pitch_mode).grid(row=index, column=0, sticky="w", pady=5)
            else:
                ttk.Label(sizes.body, text=label).grid(row=index, column=0, sticky="w", pady=5)
            entry = ttk.Entry(sizes.body, textvariable=self.values[key], width=22)
            entry.grid(row=index, column=1, sticky="ew", padx=(18, 0), pady=5)
            self.entries[key] = entry
        sizes.body.columnconfigure(1, weight=1)

        self.preview = tk.Canvas(body, height=112, background=COLORS["bg"], highlightthickness=0)
        self.preview.pack(fill="x", pady=(0, 7))
        self.preview.bind("<Configure>", lambda _event: self.update_preview())
        ttk.Label(body, textvariable=self.preview_note, style="Muted.TLabel", wraplength=680, justify="left").pack(anchor="w", fill="x")

        if not create:
            frame = ttk.Frame(body); frame.pack(fill="x", pady=(16, 4))
            ttk.Label(frame, text="应用到掩模步骤").pack(side="left", padx=(0, 14))
            for index, step in enumerate(app.data["process"]["steps"]):
                if step.get("operation") in ("add", "etch"):
                    label = f"{index+1:02d} · {step.get('name', step.get('id', '未命名'))} [{step.get('id', '')}] · {step['operation']}"
                    self.targets[label] = step.get("id")
            self.target_combo = ttk.Combobox(frame, textvariable=self.target_var, values=list(self.targets), state="readonly")
            self.target_combo.pack(side="left", fill="x", expand=True)
            target_id = app.data["process"].get("mark", {}).get("target_step_id")
            selected = app.step_index
            if target_id is None and selected is not None:
                target_id = app.data["process"]["steps"][selected].get("id")
            match = next((label for label, value in self.targets.items() if value == target_id), None)
            if match: self.target_var.set(match)
            elif len(self.targets) == 1: self.target_var.set(next(iter(self.targets)))
            ttk.Label(body, text="模板高度写入选中步骤；重建其掩模，CD 偏置 / 位移 / 旋转归零，侧壁设为 90°，同步周期及光路预设。\n该步骤的材料、起始 z、操作类型及其余步骤保留；原始 GDS 保留。应用后请重新生成几何与 FDTD。", style="Muted.TLabel", wraplength=680, justify="left").pack(anchor="w", pady=(4, 8))
        else:
            ttk.Label(body, text="新工程使用清楚标注的 DEMO 材料。尺寸和结构需与实际样品核对；随后可在工艺页修改材料与层叠。", style="Muted.TLabel", wraplength=680, justify="left").pack(anchor="w", pady=(16, 8))
        ttk.Label(footer, textvariable=self.error, foreground=COLORS["danger"], wraplength=680, justify="left").pack(fill="x", pady=(0, 4))
        actions = ttk.Frame(footer); actions.pack(fill="x")
        ttk.Label(actions, text="此窗口的输入在点击应用前不会生效。", style="Muted.TLabel").pack(side="left")
        self.apply_button = ttk.Button(actions, text="创建工程…" if create else "应用 Mark 参数", style="Primary.TButton", command=self.apply)
        self.apply_button.pack(side="right")
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="right", padx=10)
        self.bind("<Escape>", lambda _event: self.destroy())
        for variable in self.values.values():
            variable.trace_add("write", lambda *_args: self.update_preview())
        initial = app.data["process"].get("mark") if app.data and not create else None
        self._loading = False
        if initial:
            self.type_var.set(initial.get("mark_type", ""))
            self._load_config(initial)
        elif create:
            self.type_var.set("AH53"); self.change_type()
        else:
            self.type_var.set(""); self.update_preview()

    def _load_config(self, config):
        self._loading = True
        try:
            self._active_type = config["mark_type"]
            order = self.catalog.get(self._active_type, {}).get("target_order")
            self.values["mother"].set(f"{config['mother_period_um']:.12g}")
            self.values["subpitch"].set(f"{config['mother_period_um']/order:.12g}" if order else "")
            self.values["cd"].set(f"{1000*config['cd_um']:.12g}")
            self.values["height"].set(f"{1000*config['height_um']:.12g}")
        finally:
            self._loading = False
        self.update_preview()

    def change_type(self, _event=None):
        if self._active_type:
            self._drafts[self._active_type] = {key: value.get() for key, value in self.values.items()}
        selected = self._selected_type()
        self._active_type = selected
        if selected in self._drafts:
            self._loading = True
            try:
                for key, value in self._drafts[selected].items(): self.values[key].set(value)
            finally: self._loading = False
            self.update_preview()
            return
        try:
            self._load_config(marks.default_mark_config(selected))
        except (ValueError, KeyError, NotImplementedError):
            self.update_preview()

    def reset_defaults(self):
        try:
            self._load_config(marks.default_mark_config(self._selected_type()))
        except (ValueError, KeyError, NotImplementedError) as exc:
            self.error.set(str(exc))

    def change_pitch_mode(self):
        # The inactive field was kept in sync while typing. Switching ownership
        # therefore preserves the same geometry instead of silently changing L.
        self.update_preview()

    def _selected_type(self):
        text = self.type_var.get()
        return next((key for key, entry in self.catalog.items() if entry["label"] == text), text)

    def read_config(self):
        kind = self._selected_type()
        if not kind: raise ValueError("请选择 Mark 类型；程序不会根据 GDS 文件名推断。")
        entry = self.catalog[kind]
        if entry.get("status") == "reserved":
            raise NotImplementedError("此 Mark 类型仅预留，几何与光路算法尚未实现，不能计算。")
        try:
            period = float(self.values["mother"].get()) if self.pitch_mode.get() == "mother" else float(self.values["subpitch"].get()) * entry["target_order"]
            return marks.normalize_mark_config({"schema_version": "1.0", "mark_type": kind,
                "mother_period_um": period, "cd_um": float(self.values["cd"].get()) / 1000,
                "height_um": float(self.values["height"].get()) / 1000})
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"尺寸无效：{exc}") from exc

    def update_preview(self):
        if self._loading: return
        self.preview.delete("all")
        try:
            config = self.read_config()
        except (ValueError, KeyError, NotImplementedError) as exc:
            self.error.set(str(exc)); self.preview_note.set("参数未应用 · 请先完成有效输入。")
            self.apply_button.configure(state="disabled")
            return
        self.error.set(""); self.apply_button.configure(state="normal")
        self._loading = True
        try:
            inactive = "subpitch" if self.pitch_mode.get() == "mother" else "mother"
            self.values[inactive].set(f"{config['subpitch_um'] if inactive == 'subpitch' else config['mother_period_um']:.12g}")
            self.entries["mother"].configure(state="normal" if self.pitch_mode.get() == "mother" else "readonly")
            self.entries["subpitch"].configure(state="normal" if self.pitch_mode.get() == "subpitch" else "readonly")
        finally: self._loading = False
        width = max(self.preview.winfo_width(), 400); margin = 30; scale = (width-2*margin)/config["mother_period_um"]
        self.preview.create_line(margin, 86, width-margin, 86, fill=COLORS["field_border"], arrow="both")
        self.preview.create_text(width/2, 101, text=f"母周期 L = {config['mother_period_um']:g} µm", fill=COLORS["muted"])
        for center in config["centers_um"]:
            x1 = width/2 + (center-config["cd_um"]/2)*scale
            x2 = width/2 + (center+config["cd_um"]/2)*scale
            self.preview.create_rectangle(x1, 17, x2, 72, fill=COLORS["primary"], outline="")
        self.preview_note.set(f"结构示意（俯视，非仿真结果）：{config['stripe_count']} 条纹；填充率 {100*config['fill_fraction']:.2f}%。\n"
            f"目标结构阶次 ±{config['target_order']}；实际各阶幅值由几何、材料及 FDTD 决定。")

    def apply(self):
        """Commit only a fully valid draft; keep the dialog on actionable error."""
        try:
            if self.app.busy: raise ValueError("任务正在运行，请完成或取消后设置 Mark")
            if not self.create and (self.app.data is None or self.app.data["folder"] != self.source_folder):
                raise ValueError("当前工程已切换，请关闭此窗口并重新打开 Mark 编辑器。")
            config = self.read_config()
            if self.create:
                parent = filedialog.askdirectory(parent=self, title="选择新工程的父目录（自动创建唯一子目录）")
                if not parent: return None
                target = service.unique_folder(Path(parent), f"{config['mark_type']}_DEMO")
                marks.create_mark_project(target, config["mark_type"], parameters=config)
                self.app.open_project(target)
                from .usability import remember_project
                remember_project(target)
            else:
                target_id = self.targets.get(self.target_var.get())
                if not target_id: raise ValueError("请选择要替换的 add / etch 掩模步骤；其余步骤将保留。")
                # Preserve other valid form edits before refreshing UI variables.
                incoming = self.app.collect()
                changed = marks.apply_mark_to_project(incoming, config, target_step_id=target_id)
                self.app.loading = True
                try:
                    self.app.data = changed
                    self.app._load_project_fields()
                finally: self.app.loading = False
                self.app.mark_changed("physics")
                self.app.save_draft()
                self.app.status.set(f"{config['mark_type']} 参数已应用到工程草稿；请生成几何、重新运行 FDTD，并保存工程。")
            self.destroy()
            return config
        except Exception as exc:
            self.error.set(f"{type(exc).__name__}: {exc}")
            return None


def show_mark_editor(app, *, create=False):
    if not create: app._assert_recipe_editable()
    previous = getattr(app, "_mark_editor", None)
    if previous is not None and previous.winfo_exists():
        previous.lift(); previous.focus_set()
        return previous
    app._mark_editor = MarkEditor(app, create=create)
    return app._mark_editor
