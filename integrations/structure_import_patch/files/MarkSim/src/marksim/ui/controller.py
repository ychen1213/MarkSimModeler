"""Input binding, invalidation and asynchronous service dispatch for the Tk owner."""
from __future__ import annotations
import copy
from pathlib import Path
import queue
import threading

from .. import service
from ..jobs import load_runtime
from ..schemas import canonical_hash, simulation_config


class TaskController:
    """Feature methods sharing the single MarkSimApp state owner."""

    def mark_changed(self, group):
        if self.loading or self.data is None: return
        if group == "physics":
            self.prepared = None; self.prepared_binding = None
            self.invalidate_process_geometry()
            if self.scattering is not None: self.stale = True
            self.status.set("物理输入已变更：请重新验证 / 生成几何；旧结果已过期。")
            if self.scattering is not None: self.result_status.set("已过期：当前物理输入与所显示矩阵不再绑定。原始结果仍保留。")
            if self.last_optics is not None: self.optics_result_status.set("物理输入已修改：下方旧读出不再代表当前工程，请重新计算。")
        else:
            self.status.set("光路参数已变更：可复用当前有效矩阵生成新的读出。")
            self.optics_result_status.set("光路参数已修改：下方旧曲线仍对应其保存配置，请重新生成光路扫描。")
        self._states()

    def _states(self):
        exists = self.data is not None
        for key in ("validate", "geometry", "full", "scan", "reconstruction"):
            self._buttons[key].configure(state="normal" if exists and not self.busy else "disabled")
        self._buttons["fdtd"].configure(state="normal" if exists and self.prepared is not None and not self.busy else "disabled")
        valid = self.scattering is not None and not self.stale and self.scattering["metadata"].get("time_converged", False)
        self._buttons["optics"].configure(state="normal" if exists and valid and not self.busy else "disabled")
        if exists and hasattr(self, "_apply_imported_controls"): self._apply_imported_controls()
        self._buttons["cancel"].configure(state="normal" if self.busy else "disabled")
        from .shell import update_workflow
        update_workflow(self)

    def collect(self, *, include_physics=True):
        if self.data is None: raise ValueError("请先打开工程")
        data = copy.deepcopy(self.data)
        if include_physics:
            for key, var in self.sim_vars.items(): data["simulation"][key] = self._number(var, key)
            data["simulation"] = simulation_config(data["simulation"])
        for key, var in self.optics_vars.items():
            data["optics"][key] = self._number(var, key, integer=key in ("max_abs_order", "scan_num_points"))
        if data["optics"]["scan_num_points"] < 2: raise ValueError("扫描点数至少为 2")
        from ..structure_import import is_imported, imported_model
        from ..schemas import read_json
        formal = read_json(Path(data["folder"])/"project.yaml")
        if formal.get("geometry_input") != data["project"].get("geometry_input"):
            raise ValueError("不能通过内存草稿切换结构输入源；请导入新工程")
        if is_imported(data): imported_model(data)
        return data

    def _number(self, variable, key, *, integer=False):
        import math
        try:
            value = int(variable.get()) if integer else float(variable.get())
            if not math.isfinite(value): raise ValueError("nonfinite")
            return value
        except (ValueError, OverflowError):
            label = getattr(self, "field_labels", {}).get(key, key)
            entry = getattr(self, "field_entries", {}).get(key)
            if entry is not None: entry.focus_set(); entry.selection_range(0, "end")
            raise ValueError(f"{label}：请填写有效的{'整数' if integer else '有限数值'}，当前为“{variable.get()}”") from None

    def fingerprint(self, data=None):
        data = data or self.collect()
        from ..structure_import import is_imported, input_binding
        if is_imported(data): return input_binding(data)
        from ..schemas import file_hash
        files = {}
        for step in data["process"].get("steps", []):
            path = step.get("mask", {}).get("gds_path")
            if path:
                from ..paths import resolve_input_path
                p = resolve_input_path(path, base_dir=data["folder"])
                files[str(p)] = file_hash(p) if p.is_file() else "missing"
        # Same project-local source resolution and actual raw-TXT reread as CLI.
        library = service.refresh_material_sources(data["materials"], project_dir=data["folder"])
        return canonical_hash({"process": data["process"], "materials": library, "simulation": data["simulation"], "source_files": files})

    def start_job(self, kind):
        if self.busy: raise ValueError("已有任务正在运行")
        external_optics = kind == "optics" and self.scattering_binding is None
        data = self.collect(include_physics=not external_optics)
        runtime = load_runtime(self.runtime_path.get()) if kind in ("fdtd", "full", "scan") else None
        binding = None if external_optics else self.fingerprint(data)
        prepared = self.prepared
        if kind == "fdtd" and prepared is None: raise ValueError("先验证设置或生成几何")
        if kind == "fdtd" and binding != self.prepared_binding:
            self.mark_changed("physics"); raise ValueError("验证后 GDS / 材料 / 设置发生变化，请重新验证当前输入")
        if kind == "optics" and self.scattering_binding is not None and binding != self.scattering_binding:
            self.mark_changed("physics"); raise ValueError("已绑定矩阵的原始输入已变化，不能作为当前输入的光路结果")
        if kind == "optics" and (self.scattering_file is None or self.stale): raise ValueError("选择当前有效的已校正矩阵")
        scanpath = self.scan_path.get().strip(); values = []
        if kind == "scan":
            import math
            try:
                values = [float(value.strip()) for value in self.scan_values.get().replace("，", ",").split(",")]
                if not values or not all(math.isfinite(value) for value in values): raise ValueError("nonfinite")
            except ValueError:
                raise ValueError("扫描取值：请输入逗号分隔的有限数值，例如 0.25, 0.30, 0.35") from None
            try: service._set_scan_value(copy.deepcopy(data), scanpath, values[0])
            except (ValueError, IndexError, KeyError) as exc:
                raise ValueError(f"扫描参数路径不可用：{scanpath}。请核对步骤编号和支持的参数。{exc}") from exc
        if kind in ("full", "scan"): self.save_project(); data = self.collect()
        path = self.scattering_file; settings = copy.deepcopy(self.reconstruction_settings)
        self.busy = True; self.cancel_event.clear(); self.last_result = None; self.last_error = None
        self.error.set(""); self.status.set("运行中："+kind+"；界面保持可操作，结果独立保存。")
        self.progress.start(12); self._states()
        def log(text): self.messages.put(("log", str(text)))
        def work():
            try:
                kwargs = {"runtime": runtime, "cancel_event": self.cancel_event, "log": log}
                if kind in ("validate", "geometry"):
                    result = service.prepare_project(data["folder"], process=data["process"], simulation=data["simulation"], materials=data["materials"], save_artifacts=kind=="geometry", log=log)
                elif kind == "fdtd": result = service.run_fdtd(data["folder"], prepared=prepared, **kwargs)
                elif kind == "full": result = service.run_full(data["folder"], **kwargs)
                elif kind == "scan": result = service.scan_parameter(data["folder"], scanpath, values, **kwargs)
                elif kind == "optics":
                    config = copy.deepcopy(data["optics"])
                    if config.get("reference_scattering") and not Path(config["reference_scattering"]).is_absolute(): config["reference_scattering"] = str(data["folder"]/config["reference_scattering"])
                    result = service.optics_only(path, config)
                elif kind == "reconstruction":
                    folder = service.unique_folder(data["folder"]/"reconstruction", "run")
                    wrapper = {"schema_version": "1.0", "legacy_config": settings["config"], **{k: settings[k] for k in ("material_map", "tem_section", "registration", "spacing_um")}}
                    result = service.reconstruct_images(wrapper, folder, log=log, cancel_event=self.cancel_event)
                else: raise ValueError("Unknown action")
                self.messages.put(("done", (kind, result, binding)))
            except Exception as exc:
                import traceback
                self.messages.put(("log", f"阶段 {kind} 失败：\n{traceback.format_exc()}"))
                self.messages.put(("error", f"{type(exc).__name__}: {exc}"))
        self.thread = threading.Thread(target=work, daemon=True); self.thread.start()

    def _poll(self):
        try:
            while True:
                kind, content = self.messages.get_nowait()
                if kind == "log": self.append_log(content)
                else:
                    self.busy = False; self.progress.stop()
                    if kind == "error":
                        self.last_error = content; self.error.set(content); self.append_log(content); self.status.set("任务失败；实际原因和已完成文件已保留。")
                    else:
                        action, result, binding = content; self.last_result = result
                        self.safe(lambda: self.accept_result(action, result, binding))
                    self._states()
        except queue.Empty: pass
        if self.pending_close and not self.busy: self.close(); return
        self._poll_after = self.after(100, self._poll)

    def accept_result(self, action, result, binding):
        if action in ("validate", "geometry"):
            self.prepared = result if self.fingerprint() == binding else None
            self.prepared_binding = binding if self.prepared is not None else None
            self.resource_info.set(f"已验证：约 {result['estimated_cells']:,} 网格单元，基础内存估算 {result['estimated_memory_mb']:.1f} MB（实际峰值需另测）；串行 1 进程。")
            if result.get("artifact_dir"):
                self.show_geometry(result["artifact_dir"], result["task"]["geometry"]); self.tabs.select(2)
            self.status.set("输入与材料几何验证通过；可以运行真实 FDTD。" if self.prepared is not None else "计算中输入发生变化：已显示上次几何，请重新验证当前输入。")
        elif action in ("fdtd", "full"):
            run = result["fdtd"] if action == "full" else result
            self.refresh_history()
            if run["status"] == "succeeded":
                self.load_result(Path(run["run_dir"])/"scattering.h5", binding=binding)
                self.status.set("FDTD 已完成；数值收敛状态以散射结果和独立验收报告为准。")
                if action == "full" and result.get("optics"): self.show_optics(result["optics"])
                else: self.tabs.select(6)
            else: self.status.set("任务状态："+run["status"]); self.error.set(run.get("error", ""))
        elif action == "optics": self.show_optics(result)
        elif action == "scan": self.refresh_history(); self.status.set(f"扫描 {result['status']}：{len(result['results'])}/{result['task_count']} 点，{result['queue_dir']}")
        elif action == "reconstruction":
            self._show_sem_preview(result.get("preview_files", [])); self.sem_description.set("统一体素已输出："+str(result.get("voxels_file"))+"\n质量："+str(result.get("quality"))); self.status.set("SEM/TEM 几何桥接完成；尚未进入周期 Jones 求解。")
        self.save_state(); self._states()

    def cancel(self):
        self.cancel_event.set(); self.status.set("已请求取消；等待任务安全退出并保存状态。")
