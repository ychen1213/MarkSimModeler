"""Project opening, revision-safe saving, draft restore and configuration editing."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import shutil
import tkinter as tk
from tkinter import filedialog, ttk

from ... import service
from ...schemas import read_json, write_json, simulation_config, versioned
from ...paths import resolve_input_path
from ..widgets import JsonEditor


class ProjectSection:
    """Feature methods sharing the single MarkSimApp state owner."""

    def _project_page(self):
        page = self.pages[0]
        row = ttk.Frame(page); row.pack(fill="x")
        ttk.Entry(row, textvariable=self.project_path, state="readonly").pack(side="left", fill="x", expand=True)
        self.button(row, "打开工程", self.choose_project)
        self.button(row, "导入结构用于仿真…", self.choose_structure_bundle)
        self.button(row, "新建 Mark 工程…", self.create_demo)
        row = ttk.Frame(page); row.pack(fill="x")
        self.button(row, "另存工程", self.save_as)
        self.button(row, "工程说明 JSON", lambda: self.edit_json("project"))
        ttk.Label(page, text="保存会保留配置修订。DEMO 材料用于链路验证；请选择符合实际样品的数据。", wraplength=780).pack(anchor="w", pady=8)
        self.structure_mode = tk.StringVar(value="输入：版图与工艺")
        ttk.Label(page, textvariable=self.structure_mode, wraplength=850, foreground="#8b5a13").pack(anchor="w", pady=6)
        from ..controls import Collapsible
        self.material_import = Collapsible(page, title="导入自己的材料 TXT"); self.material_import.pack(fill="x", pady=(0, 8))
        importbar = ttk.Frame(self.material_import.body, padding=8); importbar.pack(fill="x")
        self.material_unit = tk.StringVar(value="nm")
        ttk.Label(importbar, text="波长单位").pack(side="left")
        ttk.Combobox(importbar, textvariable=self.material_unit, values=["nm", "um", "m"], state="readonly", width=6).pack(side="left", padx=5)
        self.material_columns = tk.StringVar(value="1,2,3")
        ttk.Label(importbar, text="λ,n,k 列（无 k 填 1,2）").pack(side="left")
        ttk.Entry(importbar, textvariable=self.material_columns, width=12).pack(side="left", padx=5)
        self.assume_lossless = tk.BooleanVar(value=False)
        extra = ttk.Frame(self.material_import.body, padding=(8,0)); extra.pack(fill="x")
        ttk.Checkbutton(extra, text="k 未知时，明确按无吸收近似", variable=self.assume_lossless).pack(side="left", padx=8)
        self.button(extra, "导入文件夹", self.import_materials)
        ttk.Label(page, text="材料库 · 选择一行查看 n、k 曲线", style="Section.TLabel").pack(anchor="w", pady=5)
        self.material_tree = ttk.Treeview(page, columns=("range", "k", "source"), show="tree headings", height=5)
        self.material_tree.heading("#0", text="材料名称"); self.material_tree.heading("range", text="有效波段 nm")
        self.material_tree.heading("k", text="k / 吸收状态"); self.material_tree.heading("source", text="原始数据来源")
        self.material_tree.column("#0", width=140); self.material_tree.column("range", width=130); self.material_tree.column("k", width=160); self.material_tree.column("source", width=260)
        self.material_tree.pack(fill="x", pady=8); self.material_tree.bind("<<TreeviewSelect>>", lambda _e: self.safe(self.plot_material))
        self.material_plot = ttk.Frame(page); self.material_plot.pack(fill="both", expand=True)
        self.material_canvas = None

    def _config_file_hashes(self, data):
        from ...schemas import file_hash
        names = ["project.yaml"] + [data["project"][key] for key in ("process", "materials", "simulation")]
        hashes = {name: file_hash(data["folder"]/name) for name in names if (data["folder"]/name).is_file()}
        from ...structure_import import is_imported, imported_model
        if is_imported(data):
            model = imported_model(data)
            hashes["structure_manifest"] = model["manifest_sha256"]
            hashes.update({"structure:"+key:value for key,value in model["manifest"]["files_sha256"].items()})
        return hashes

    def save_draft(self):
        if self.data is None: return
        self._preserve_failed_restore(self.data["folder"] / "ui_draft.json")
        write_json(self.data["folder"]/"ui_draft.json", {"schema_version": "1.0", "base_file_hashes": self.base_file_hashes,
            "data": {key: self.data[key] for key in ("project", "process", "materials", "simulation")},
            "simulation_fields": {key: var.get() for key, var in self.sim_vars.items()},
            "step_index": self.step_index, "pending_step_json": self.step_text.get("1.0", "end"),
            "step_fields": {key: var.get() for key, var in self.step_vars.items()}, "step_enabled": self.step_enabled.get()})

    def open_project(self, path):
        if self.busy: raise ValueError("任务正在运行，请完成或取消后切换工程")
        # Read and validate the formal project before changing the state owner.
        # A failed open must leave the previous project and its form values intact.
        incoming = service.load_project(path)
        self._validate_view_data(incoming)
        base_hashes = self._config_file_hashes(incoming)
        if self.data is not None: self.save_draft()
        draft = self._read_optional_state(incoming, "ui_draft.json", base_hashes)
        state = self._read_optional_state(incoming, "ui_state.json", base_hashes)
        if not hasattr(self, "_reconstruction_defaults"):
            self._reconstruction_defaults = copy.deepcopy(self.reconstruction_settings)
        self.loading = True
        try:
            self.data = incoming; self.project_path.set(str(incoming["folder"]))
            # Restore editable widget states before applying a recipe draft.
            self._apply_imported_controls()
            self.prepared = None; self.prepared_binding = None; self.last_result = None
            self._clear_restored_results()
            self._clear_restored_geometry()
            self.sem_image.clear()
            self.reconstruction_settings = copy.deepcopy(self._reconstruction_defaults)
            if self.material_canvas: self.material_canvas.get_tk_widget().destroy(); self.material_canvas = None
            self.base_file_hashes = base_hashes
            self._load_project_fields()
            if draft:
                try:
                    self.data = {**incoming, **copy.deepcopy(draft["data"])}
                    self._load_project_fields()
                    for key, value in draft.get("simulation_fields", {}).items(): self.sim_vars[key].set(value)
                    index = draft.get("step_index")
                    if index is not None:
                        self.process_tree.selection_set(str(index)); self.select_step()
                        self.step_text.delete("1.0", "end"); self.step_text.insert("1.0", draft.get("pending_step_json", ""))
                        for key, value in draft.get("step_fields", {}).items(): self.step_vars[key].set(value)
                        self.step_enabled.set(draft.get("step_enabled", True))
                except Exception as exc:
                    self.data = incoming
                    self._load_project_fields()
                    self._mark_restore_failure(incoming["folder"] / "ui_draft.json")
                    self.append_log(f"ui_draft.json 恢复失败，已使用正式工程配置；原文件保留：{exc}")
            if state: self._restore_project_state(state)
            from ...structure_import import is_imported, preview_snapshot
            if is_imported(self.data):
                preview = service.unique_folder(self.data["folder"] / "geometry", "imported_view")
                result = preview_snapshot(self.data, preview)
                self.show_geometry(result["artifact_dir"], result["geometry"])
            self._apply_imported_controls()
            self.status.set(f"工程已打开：{self.data['project'].get('name', '')}")
        finally:
            self.loading = False; self._states()

    @staticmethod
    def _validate_view_data(data):
        """Validate only the structures needed to display a project, not its physics."""
        for key in ("project", "process", "materials", "simulation"):
            if not isinstance(data.get(key), dict): raise ValueError(f"{key} 必须是配置对象")
            versioned(data[key], key)
        simulation_config(data["simulation"])
        if data.get("folder"):
            formal = read_json(Path(data["folder"])/"project.yaml")
            if formal.get("geometry_input") != data["project"].get("geometry_input"):
                raise ValueError("不能通过草稿或工程JSON切换结构输入源；请导入新工程")
            from ...structure_import import is_imported, imported_model
            if is_imported(data): imported_model(data)
        for key in ("process", "materials", "simulation"):
            if not isinstance(data["project"].get(key), str): raise ValueError(f"project.{key} 需要配置文件路径")
        steps = data["process"].get("steps")
        if not isinstance(steps, list): raise ValueError("process.steps 必须是步骤列表")
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or not isinstance(step.get("operation"), str):
                raise ValueError(f"process.steps[{index}] 需要 operation 字段")
            targets = step.get("target_materials", [])
            if not isinstance(targets, list) or not all(isinstance(item, str) for item in targets):
                raise ValueError(f"process.steps[{index}].target_materials 必须是材料名称列表")
        library = data["materials"].get("materials")
        if not isinstance(library, dict) or not all(isinstance(value, dict) for value in library.values()):
            raise ValueError("materials.materials 必须是材料表对象")

    def _read_optional_state(self, incoming, filename, base_hashes):
        path = incoming["folder"] / filename
        if not path.is_file(): return None
        try:
            value = read_json(path)
            if not isinstance(value, dict): raise ValueError("需要 JSON 对象")
            versioned(value, filename)
            if filename == "ui_draft.json":
                if value.get("base_file_hashes") != base_hashes:
                    self.append_log("检测到工程文件外部变化：保留 ui_draft.json，未自动覆盖新配置。")
                    return None
                saved = value.get("data")
                keys = {"project", "process", "materials", "simulation"}
                if not isinstance(saved, dict) or set(saved) != keys:
                    raise ValueError("草稿 data 必须包含四组配置，不能覆盖工程目录")
                self._validate_view_data({**incoming, **saved})
                for field, allowed in (("simulation_fields", self.sim_vars), ("step_fields", self.step_vars)):
                    fields = value.get(field, {})
                    if not isinstance(fields, dict) or set(fields) - set(allowed):
                        raise ValueError(f"{field} 含未知字段或格式不正确")
                    if any(not isinstance(item, (str, int, float)) for item in fields.values()):
                        raise ValueError(f"{field} 必须是可显示的表单文本")
                index = value.get("step_index")
                if index is not None and (type(index) is not int or not 0 <= index < len(saved["process"]["steps"])):
                    raise ValueError("step_index 不是有效步骤索引")
                if not isinstance(value.get("pending_step_json", ""), str) or type(value.get("step_enabled", True)) is not bool:
                    raise ValueError("步骤草稿文本 / 启用状态格式不正确")
            else:
                for key in ("scattering_file", "scattering_binding", "geometry_folder"):
                    if value.get(key) is not None and not isinstance(value[key], str):
                        raise ValueError(f"{key} 必须是字符串或 null")
                if "reconstruction" in value:
                    reconstruction = value["reconstruction"]
                    if not isinstance(reconstruction, dict) or not {"config", "material_map", "tem_section", "registration", "spacing_um"} <= set(reconstruction):
                        raise ValueError("reconstruction 恢复设置缺少必要字段")
            return value
        except Exception as exc:
            self._mark_restore_failure(path)
            self.append_log(f"{filename} 恢复失败，已忽略并使用正式工程配置；原文件保留：{type(exc).__name__}: {exc}")
            return None

    def _mark_restore_failure(self, path):
        if not hasattr(self, "_failed_restore_files"): self._failed_restore_files = set()
        self._failed_restore_files.add(Path(path).resolve())

    def _preserve_failed_restore(self, path):
        """Keep corrupt evidence before a later deliberate/autosave replaces it."""
        path = Path(path).resolve()
        pending = getattr(self, "_failed_restore_files", set())
        if path not in pending: return
        if path.is_file():
            archive = service.unique_folder(path.parent / "config_revisions", "ui_recovery")
            shutil.copy2(path, archive / path.name)
            self.append_log(f"损坏恢复文件已原样备份，随后保存当前界面状态：{archive / path.name}")
        pending.discard(path)

    def _load_project_fields(self):
        """Load a complete set, including defaults, without reusing the prior project."""
        for key, var in self.sim_vars.items(): var.set(str(self.data["simulation"][key]))
        self.step_text.delete("1.0", "end")
        for var in self.step_vars.values(): var.set("")
        self.step_enabled.set(True)
        self.refresh_materials(); self.refresh_steps(); self.refresh_history()
        from ..mark_editor import refresh_mark_labels
        refresh_mark_labels(self)

    def _clear_restored_results(self):
        self.scattering = None; self.scattering_file = None; self.scattering_binding = None; self.stale = False
        self.result_status.set("未计算")
        self.result_physics_summary.set("")
        self.result_image.clear()

    def _clear_restored_geometry(self):
        self.geometry_folder = None
        self.geometry_tree.delete(*self.geometry_tree.get_children())
        self.geometry_image.clear()
        self.clear_process_geometry()
        self.geometry_description.set("尚未生成当前工程的几何。")

    def _restore_project_state(self, state):
        self.reconstruction_settings = copy.deepcopy(state.get("reconstruction", self._reconstruction_defaults))
        path = state.get("scattering_file")
        if path:
            source = resolve_input_path(path, base_dir=self.data["folder"])
            try:
                self.load_result(source, binding=state.get("scattering_binding"), restored=True)
            except Exception as exc:
                self._clear_restored_results()
                self._mark_restore_failure(self.data["folder"] / "ui_state.json")
                self.append_log(f"ui_state.json 散射结果恢复失败，已清空预览；原文件保留：{source}；{exc}")
        folder = state.get("geometry_folder")
        if folder:
            source = resolve_input_path(folder, base_dir=self.data["folder"])
            try:
                from ...schemas import validate_geometry
                geometry = validate_geometry(read_json(source / "geometry.json"))
                self.show_geometry(source, geometry)
            except Exception as exc:
                self._clear_restored_geometry()
                self._mark_restore_failure(self.data["folder"] / "ui_state.json")
                self.append_log(f"ui_state.json 几何恢复失败，已清空预览；原文件保留：{source}；{exc}")

    def choose_project(self):
        from ..usability import choose_project
        choose_project(self)

    def create_demo(self):
        from ..mark_editor import show_mark_editor
        return show_mark_editor(self, create=True)

    def save_state(self):
        if self.data:
            self._preserve_failed_restore(self.data["folder"] / "ui_state.json")
            write_json(self.data["folder"]/"ui_state.json", {"schema_version": "1.0", "scattering_file": str(self.scattering_file) if self.scattering_file else None,
                "scattering_binding": self.scattering_binding, "geometry_folder": str(self.geometry_folder) if self.geometry_folder else None,
                "reconstruction": self.reconstruction_settings})

    def save_project(self):
        if self.busy: raise ValueError("任务运行中不改写工程配置")
        data = self.collect(); root = data["folder"]
        targets = self._save_targets(data, root)
        revision = service.unique_folder(root/"config_revisions", "revision")
        for key, filename in targets:
            original = root/filename
            if original.is_file():
                backup = revision/filename; backup.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(original, backup)
            write_json(original, data[key])
        self.data = data; self.base_file_hashes = self._config_file_hashes(data); self.save_draft(); self.save_state(); self.status.set("配置已保存；原始输入未改写，旧配置已存入 config_revisions。")
        return root

    def _save_targets(self, data, root):
        root = Path(root).resolve()
        targets = [("project", "project.yaml")]+[(key, data["project"][key]) for key in ("process", "simulation", "materials")]
        seen = set()
        raw_sources = {Path(item["source"]).resolve() for item in data["materials"]["materials"].values() if item.get("source") and Path(item["source"]).is_file()}
        for step in data["process"].get("steps", []):
            value = step.get("mask", {}).get("gds_path")
            if value: raw_sources.add((root/Path(value)).resolve())
        for _key, name in targets:
            path = (root/name).resolve()
            if not path.is_relative_to(root) or path.suffix.lower() not in (".json", ".yaml", ".yml"):
                raise ValueError("配置输出必须是工程内部的 .json/.yaml/.yml，禁止覆盖原始文件或工程外路径")
            if "inputs" in path.relative_to(root).parts or path in raw_sources or path in seen:
                raise ValueError("配置路径不能指向 inputs、原始材料/GDS 或与另一配置同名")
            seen.add(path)
        return targets

    def save_as(self):
        if self.busy: raise ValueError("任务正在运行，请完成或取消后另存工程")
        if self.data is None: raise ValueError("请先打开工程")
        parent = filedialog.askdirectory(title="另存为：选择新工程父目录")
        if not parent: return
        if Path(parent).resolve().is_relative_to(self.data["folder"].resolve()):
            raise ValueError("另存工程的父目录不能在当前工程内部，避免递归复制")
        data = self.collect(); targets = self._save_targets(data, data["folder"])
        target = service.unique_folder(parent, self.data["project"].get("name", "MarkSim"))
        for entry in self.data["folder"].iterdir():
            if entry.name in ("runs", "queues", "config_revisions", "geometry", "ui_state.json", "ui_draft.json"): continue
            if entry.is_dir(): shutil.copytree(entry, target/entry.name)
            else: shutil.copy2(entry, target/entry.name)
        for key, filename in targets: write_json(target/filename, data[key])
        self.open_project(target)

    def edit_json(self, key):
        if key in ("process", "materials"): self._assert_recipe_editable()
        current = self.collect()
        def apply(value):
            if not isinstance(value, dict): raise ValueError("需要 JSON 对象")
            if key == "simulation": value = simulation_config(value)
            if key == "project" and value.get("geometry_input") != current["project"].get("geometry_input"):
                raise ValueError("不能在工程 JSON 切换结构输入源；请使用导入新工程")
            self.data[key] = value
            self.loading = True
            try:
                variables = self.sim_vars if key == "simulation" else {}
                for name, var in variables.items(): var.set(str(value.get(name, var.get())))
                self.refresh_steps(); self.refresh_materials()
            finally: self.loading = False
            self.mark_changed("physics")
        JsonEditor(self, key+" · JSON 属性", current[key], apply)

    def _assert_recipe_editable(self):
        from ...structure_import import is_imported, READ_ONLY
        if self.data and is_imported(self.data): raise ValueError(READ_ONLY)

    def _apply_imported_controls(self):
        """Show snapshot status and lock geometry/material editing, not simulation."""
        from ...structure_import import is_imported, READ_ONLY
        imported = bool(self.data and is_imported(self.data))
        self.structure_mode.set("输入：已校验结构包。"+READ_ONLY if imported else "输入：版图与工艺，可编辑建模参数。")
        if not hasattr(self, "_import_control_states"): self._import_control_states = {}
        def visit(widget):
            for child in widget.winfo_children():
                if isinstance(child, (ttk.Button, ttk.Entry, ttk.Combobox, ttk.Checkbutton, tk.Text)):
                    if imported:
                        if child not in self._import_control_states: self._import_control_states[child] = str(child.cget("state"))
                        child.configure(state="disabled")
                    elif child in self._import_control_states:
                        child.configure(state=self._import_control_states.pop(child))
                visit(child)
        visit(self.pages[1]); visit(self.material_import)
        if imported:
            self.step_form_note.set(READ_ONLY)
            self.process_dimensions["mark_type"].set("导入结构（只读）")

    def choose_structure_bundle(self):
        if self.busy: raise ValueError("请先完成或取消当前任务")
        source = filedialog.askopenfilename(title="选择建模工具导出的 structure.json", filetypes=[("Structure bundle", "structure.json")])
        if not source: return
        parent = filedialog.askdirectory(title="导入结构：选择新工程的父目录")
        if not parent: return
        target = service.unique_folder(parent, "ImportedStructure")
        result = service.import_structure(source, target, previews=False)
        self.open_project(result["project_dir"])
        self.tabs.select(2)
        self.status.set("结构已复制到新工程；当前预览即求解输入。请检查仿真设置并验证求解兼容性。")
