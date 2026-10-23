"""Image/voxel input and geometry-only exports; no electromagnetic execution."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, ttk

from .widgets import JsonDialog, card, field, hint

ROOT = Path(__file__).resolve().parents[3]


class TransferPages:
    def create_image_page(self, page):
        self._image_revision = 0
        self.image_materials = None
        box = card(page, "SEM / TEM · 图像标定与重建入口")
        hint(box, "复用已有标尺、ROI、边缘人工辅助和材料分区工具。单幅截面沿条纹延伸属于 2.5D 模型假设，不会被标记为真实三维断层测量。")
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.button(row, "打开图像标注工具", self.launch_annotation)
        self.button(row, "载入标注 / 桥接配置…", self.load_image_config)
        self.button(row, "使用已知真值合成演示", self.reset_image_config)
        self.image_spec = json.loads((ROOT / "examples" / "sem_tem_bridge_demo.json").read_text(encoding="utf-8"))
        self.image_vars = {key: tk.StringVar(value=value) for key, value in {
            "sem_nm_per_pixel": "3", "tem_nm_per_pixel": "3", "spacing": "0.02", "background": "AIR_DEMO", "substrate": "SUBSTRATE_DEMO", "ridge": "RIDGE_DEMO",
            "tem_origin": "[0, 0.66, 0]", "tem_direction": "[1, 0]", "height_reference": "0"}.items()}
        for key, variable in self.image_vars.items():
            variable.trace_add("write", lambda *_args, name=key: self._image_field_changed(name))
        form = ttk.Frame(box)
        form.pack(fill="x")
        for i, (key, label) in enumerate((("sem_nm_per_pixel", "SEM nm/px · 数值或 [x,y]"), ("tem_nm_per_pixel", "TEM nm/px · 数值或 [x,y]"), ("spacing", "体素 µm · 数值或 [x,y,z]"),
                                        ("background", "背景标签 0 材料"), ("substrate", "衬底标签 1 材料"), ("ridge", "条纹标签 2 材料"),
                                        ("tem_origin", "TEM 切面原点 [x,y,z] · µm"), ("tem_direction", "TEM 横向方向 [x,y]"), ("height_reference", "高度参考 z · µm"))):
            field(form, i // 3, i % 3, label, self.image_vars[key])
        self.image_section_confirmed = tk.BooleanVar(value=True)
        self.image_section_confirmed.trace_add("write", self._image_inputs_changed)
        self.image_calibration_confirmed = tk.BooleanVar(value=True)
        self.image_calibration_confirmed.trace_add("write", self._image_inputs_changed)
        ttk.Checkbutton(box, text="已核对本页 SEM / TEM 标尺（修改 nm/px 后需重新确认）", variable=self.image_calibration_confirmed).pack(anchor="w", pady=4)
        ttk.Checkbutton(box, text="已核对 TEM 位置 / 方向 / 高度参考（真实图必须确认）", variable=self.image_section_confirmed).pack(anchor="w", pady=4)
        self.image_note = tk.StringVar(value="当前是合成演示，未加载真实 SEM/TEM 图。")
        ttk.Label(box, textvariable=self.image_note, style="Hint.TLabel", wraplength=1050).pack(fill="x", pady=8)
        self.image_material_note = tk.StringVar(value="材料来源：已知真值合成演示材料。")
        ttk.Label(box, textvariable=self.image_material_note, style="Hint.TLabel", wraplength=1050).pack(fill="x", pady=4)
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.button(row, "选择重建材料表…", self.select_image_materials)
        self.button(row, "明确使用当前工程材料", self.use_project_image_materials)
        self.button(row, "保存当前重建配置…", self.save_image_configuration_dialog)
        hint(box, "本页配置需单独保存；顶部“保存输入”只保存工程工艺和材料。配置仅引用原图路径，不复制或修改图像。")
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.button(row, "编辑 TEM 位置 / 配准 / 标签 / ROI", self.edit_image_config)
        self.button(row, "重建并生成材料体素", self.reconstruct_async, primary=True)
        box = card(page, "已有几何 / 体素数据")
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.button(row, "加载结构包 / 几何 / H5 / NPZ…", self.load_model_dialog)
        self.button(row, "当前结构 → 材料体素", self.voxelize_async)
        hint(box, "体素不插值材料 ID，不自动平滑表面。优先采用几何旁的 materials.json；没有配套材料时必须明确选取文件，不借用当前工程的同名材料。完整三维体素可导出给 Lumerical；自家二维求解器可能拒绝求解，不能自动降维。")

    def _image_inputs_changed(self, *_):
        self._image_revision += 1
        if self.model_origin == "image" or getattr(self, "model_image_revision", None) is not None:
            self.model = None
            self.geometry_note.set("图像标尺 / 材料标签 / 重建配置已改变；重新重建后才可导出。")
            if hasattr(self, "_update_buttons"):
                self._update_buttons()

    def _image_field_changed(self, name):
        self._image_inputs_changed()
        real = not self.image_spec.get("legacy_config", {}).get("generate_demo", False)
        if real and name in ("sem_nm_per_pixel", "tem_nm_per_pixel") and hasattr(self, "image_calibration_confirmed"):
            self.image_calibration_confirmed.set(False)
        if real and name in ("tem_origin", "tem_direction", "height_reference") and hasattr(self, "image_section_confirmed"):
            self.image_section_confirmed.set(False)

    def launch_annotation(self):
        folder = ROOT / "3D reconstruction"
        path = folder / "gui.py"
        if not path.is_file():
            raise FileNotFoundError("缺少随源码提供的 SEM/TEM 标注程序")
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        subprocess.Popen([sys.executable, str(path)], cwd=folder, **kwargs)
        self.status.set("已打开本地标注工具；保存配置 JSON 后在本页载入。")

    def reset_image_config(self):
        self.image_spec = json.loads((ROOT / "examples" / "sem_tem_bridge_demo.json").read_text(encoding="utf-8"))
        self.image_materials = None
        self.update_image_form()
        self.image_note.set("已知真值合成 SEM/TEM；用于检验重建流程，不代表真实图精度。")

    @staticmethod
    def _numeric(value, label, *, sizes=(), positive=False):
        """Parse form numbers with field-specific errors; never accept NaN/bools."""
        import numpy as np
        try:
            raw = json.loads(value) if isinstance(value, str) else value
            if isinstance(raw, dict):
                if set(raw) != {"x", "y"}:
                    raise ValueError()
                raw = [raw["x"], raw["y"]]
            if isinstance(raw, bool) or (isinstance(raw, list) and any(isinstance(x, bool) for x in raw)):
                raise ValueError()
            array = np.asarray(raw, float)
            if array.shape not in sizes or not np.isfinite(array).all() or (positive and (array <= 0).any()):
                raise ValueError()
            return array.tolist()
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{label}：请输入有限{'正' if positive else ''}数；向量使用英文方括号和逗号，如 [1, 2]。") from None

    def update_image_form(self):
        legacy = self.image_spec.get("legacy_config", {})
        for key in ("sem_nm_per_pixel", "tem_nm_per_pixel"):
            self.image_vars[key].set(json.dumps(legacy.get(key, 3)))
        spacing = self._numeric(self.image_spec.get("spacing_um", [.02] * 3), "体素尺寸", sizes=((), (3,)), positive=True)
        if isinstance(spacing, (float, int)):
            spacing = [spacing] * 3
        self.image_vars["spacing"].set(str(spacing[0]) if len(set(spacing)) == 1 else json.dumps(spacing))
        for label, key in (("0", "background"), ("1", "substrate"), ("2", "ridge")):
            self.image_vars[key].set(self.image_spec.get("material_map", {}).get(label, ""))
        section = self.image_spec.get("tem_section", {})
        for key, spec_key, default in (("tem_origin", "origin_phys_um", [0, 0, 0]), ("tem_direction", "direction_xy", [1, 0]), ("height_reference", "height_reference_um", 0)):
            self.image_vars[key].set(json.dumps(section.get(spec_key, default)))
        self.image_section_confirmed.set(section.get("placement_status") != "user_input_required")
        self.image_calibration_confirmed.set(bool(legacy.get("generate_demo")) or all(legacy.get("calibration", {}).get(name, {}).get("confirmed") is True for name in ("sem", "tem")))
        confirmations = self.image_spec.get("modeler_form_confirmations", {})
        for key, variable in (("calibration", self.image_calibration_confirmed), ("tem_section", self.image_section_confirmed)):
            if type(confirmations.get(key)) is bool:
                variable.set(confirmations[key])
        self.refresh_image_material_note()

    def save_image_configuration_dialog(self):
        path = filedialog.asksaveasfilename(title="保存独立重建配置（选择新的文件名）", initialfile="reconstruction_config.json",
                                           defaultextension=".json", filetypes=[("Reconstruction configuration", "*.json")], parent=self.winfo_toplevel())
        if path:
            self.save_image_configuration(path)

    def save_image_configuration(self, path):
        """Save an independently reloadable draft without touching source images.

        Explicit project material snapshots travel in a sibling JSON. Original
        image paths remain absolute references, not an implied image bundle.
        Confirmation state is preserved even for an unconfirmed draft.
        """
        from ..materials import validate_library
        path = Path(path).resolve()
        spec = self.image_configuration()
        calibration = bool(self.image_calibration_confirmed.get())
        placement = bool(self.image_section_confirmed.get())
        spec["modeler_form_confirmations"] = {"calibration": calibration, "tem_section": placement}
        legacy = spec["legacy_config"]
        for name in ("sem", "tem"):
            image_key = name + "_image"
            if legacy.get(image_key):
                legacy[image_key] = str((ROOT / legacy[image_key]).resolve())
            previous = copy.deepcopy(legacy.get("calibration", {}).get(name, {}))
            legacy.setdefault("calibration", {})[name] = {**previous, "confirmed": calibration,
                "modeler_confirmation": {"method": "saved_form_confirmation", "nm_per_pixel": copy.deepcopy(legacy[name + "_nm_per_pixel"])}}
        if not placement:
            spec["tem_section"]["placement_status"] = "user_input_required"
        elif not legacy.get("generate_demo", False):
            spec["tem_section"]["placement_status"] = "user_confirmed"
        spec["image_file_policy"] = "absolute path references only; source images are not copied or modified"
        writes = []
        material_path = None
        if self.image_materials is not None and not spec.get("materials"):
            snapshot = validate_library(copy.deepcopy(self.image_materials))
            material_path = path.with_name(path.stem + ".materials.json")
            spec["materials"] = material_path.name
            writes.append((material_path, snapshot))
        elif isinstance(spec.get("materials"), str):
            spec["materials"] = str((ROOT / spec["materials"]).resolve())
        writes.append((path, spec))
        # Validate serializability and every destination before writing either
        # file; exclusive creation also protects against an intervening writer.
        encoded = [(target, json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n") for target, value in writes]
        for target, _text in encoded:
            if target.exists():
                raise FileExistsError(f"为保留已有文件，请选择新配置名称；此文件已经存在：{target}")
        path.parent.mkdir(parents=True, exist_ok=True)
        for target, text in encoded:
            with target.open("x", encoding="utf-8") as stream:
                stream.write(text)
        state = "已确认" if calibration and placement else "未确认草稿，重建前仍须核对标尺与位置"
        self.status.set(f"已单独保存重建配置：{path}（{state}）。原图仅引用路径，未复制；与顶部工程保存互相独立。")
        return {"config": str(path), "material_snapshot": str(material_path) if material_path else None,
                "image_policy": spec["image_file_policy"]}

    def refresh_image_material_note(self):
        if self.image_spec.get("materials"):
            source = "指定材料表：" + str(self.image_spec["materials"])
        elif self.image_materials is not None:
            source = "明确选择的工程材料快照；后续工程修改不会自动改变此快照。"
        elif self.image_spec.get("legacy_config", {}).get("generate_demo"):
            source = "已知真值合成演示材料（不借用当前工程的同名材料）。"
        else:
            source = "尚未指定；请选材料表，或明确使用当前工程材料。"
        self.image_material_note.set("材料来源：" + source)

    def select_image_materials(self):
        from ..materials import validate_library
        path = filedialog.askopenfilename(title="重建的光学材料表", filetypes=[("Material library", "*.json")], parent=self.winfo_toplevel())
        if not path:
            return
        validate_library(json.loads(Path(path).read_text(encoding="utf-8-sig")))
        self.image_spec["materials"] = str(Path(path).resolve())
        self.image_materials = None
        self._image_inputs_changed()
        self.refresh_image_material_note()

    def use_project_image_materials(self):
        self.require_project()
        self.image_materials = copy.deepcopy(self.data["materials"])
        self.image_spec.pop("materials", None)
        self._image_inputs_changed()
        self.refresh_image_material_note()

    def read_image_config(self, path):
        """Load transactionally: an invalid config must not damage current input."""
        path = Path(path).resolve()
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("图像配置应为 JSON 对象，不能是列表或数字。")
        base = path.parent
        if "legacy_config" in value:
            spec = copy.deepcopy(value)
            if isinstance(spec.get("materials"), str):
                spec["materials"] = str((base / spec["materials"]).resolve())
            legacy = spec["legacy_config"]
            if isinstance(legacy, str):
                nested = (base / legacy).resolve()
                legacy = json.loads(nested.read_text(encoding="utf-8-sig"))
                base = nested.parent
        else:
            # Raw annotation has no physical placement. Do not inherit the
            # previous demo's synthetic location, material map, or registration.
            legacy = copy.deepcopy(value)
            spec = {"schema_version": "1.0", "spacing_um": [.02] * 3,
                    "material_map": {"0": "", "1": "", "2": ""},
                    "tem_section": {"origin_phys_um": [0, 0, 0], "direction_xy": [1, 0], "height_reference_um": 0,
                                    "correspondence": "ordered", "placement_status": "user_input_required"}}
        if not isinstance(legacy, dict):
            raise ValueError("legacy_config 必须为标注对象或指向标注 JSON 的路径。")
        if type(legacy.get("generate_demo", False)) is not bool:
            raise ValueError("generate_demo 必须是 true 或 false。")
        legacy = copy.deepcopy(legacy)
        for key in ("sem_image", "tem_image"):
            if legacy.get(key):
                legacy[key] = str((base / legacy[key]).resolve())
        spec["legacy_config"] = legacy
        spacing = self._numeric(spec.get("spacing_um", [.02] * 3), "体素尺寸", sizes=((), (3,)), positive=True)
        spec["spacing_um"] = [spacing] * 3 if isinstance(spacing, (float, int)) else spacing
        if not isinstance(spec.get("tem_section"), dict) or not isinstance(spec.get("material_map"), dict):
            raise ValueError("桥接配置需要 tem_section 和 material_map 对象。")
        if set(map(str, spec["material_map"])) != {"0", "1", "2"}:
            raise ValueError("当前图像桥接支持且只支持标签 0=背景、1=衬底、2=条纹。")
        from marksim.reconstruction import validate_registration, validate_tem_section
        validate_tem_section(spec["tem_section"], validate_registration(spec.get("registration")))
        for key in ("sem_nm_per_pixel", "tem_nm_per_pixel"):
            self._numeric(legacy.get(key, 3), key, sizes=((), (2,)), positive=True)
        return spec

    def load_image_config(self):
        path = filedialog.askopenfilename(filetypes=[("Image annotation / bridge", "*.json")], parent=self.winfo_toplevel())
        if not path:
            return
        self.image_spec = self.read_image_config(path)
        self.image_materials = None
        self.update_image_form()
        mode = "合成演示" if self.image_spec["legacy_config"].get("generate_demo") else "真实图像"
        self.image_note.set(f"{mode}配置：{path}；请核对标尺、切面位置与材料标签。")

    def image_configuration(self):
        spec = copy.deepcopy(self.image_spec)
        for key in ("sem_nm_per_pixel", "tem_nm_per_pixel"):
            spec["legacy_config"][key] = self._numeric(self.image_vars[key].get(), key, sizes=((), (2,)), positive=True)
        spacing = self.voxel_spacing()
        spec["spacing_um"] = spacing
        spec.setdefault("material_map", {}).update({str(i): self.image_vars[key].get().strip() for i, key in enumerate(("background", "substrate", "ridge"))})
        if set(spec["material_map"]) != {"0", "1", "2"}:
            raise ValueError("当前图像桥接只支持标签 0=背景、1=衬底、2=条纹；不能自动推断更多材料分区。")
        section = spec.setdefault("tem_section", {})
        for key, spec_key, sizes in (("tem_origin", "origin_phys_um", ((3,),)), ("tem_direction", "direction_xy", ((2,),)), ("height_reference", "height_reference_um", ((),))):
            section[spec_key] = self._numeric(self.image_vars[key].get(), key, sizes=sizes)
        if not spec["legacy_config"].get("generate_demo") and self.image_section_confirmed.get():
            section["placement_status"] = "user_confirmed"
        from marksim.reconstruction import validate_registration, validate_tem_section
        validate_tem_section(section, validate_registration(spec.get("registration")))
        return spec

    def voxel_spacing(self):
        spacing = self._numeric(self.image_vars["spacing"].get(), "体素尺寸 µm", sizes=((), (3,)), positive=True)
        return [spacing] * 3 if isinstance(spacing, (int, float)) else spacing

    def edit_image_config(self):
        def apply(value):
            if not isinstance(value.get("legacy_config"), dict) or not value.get("tem_section") or not value.get("material_map"):
                raise ValueError("配置需要 legacy_config 对象、tem_section 和 material_map")
            if type(value["legacy_config"].get("generate_demo", False)) is not bool:
                raise ValueError("generate_demo 必须是 true 或 false。")
            if set(value["material_map"]) != {"0", "1", "2"}:
                raise ValueError("当前桥接仅支持标签 0、1、2；其他材料分区尚未实现。")
            self._numeric(value.get("spacing_um", [.02] * 3), "体素尺寸", sizes=((), (3,)), positive=True)
            for key in ("sem_nm_per_pixel", "tem_nm_per_pixel"):
                self._numeric(value["legacy_config"].get(key, 3), key, sizes=((), (2,)), positive=True)
            from marksim.reconstruction import validate_registration, validate_tem_section
            validate_tem_section(value["tem_section"], validate_registration(value.get("registration")))
            self.image_spec = value
            self.update_image_form()
        JsonDialog(self, "SEM / TEM 桥接 · 位置 / 配准 / 材料标签 / 原标注参数", self.image_configuration(), apply)

    def reconstruct_async(self):
        from .. import service
        from ..materials import validate_library
        spec = self.image_configuration()
        real_images = not spec["legacy_config"].get("generate_demo", False)
        if real_images:
            if not self.image_calibration_confirmed.get():
                raise ValueError("SEM / TEM 标尺尚未确认或刚刚改动。请根据图像标尺核对 nm/px，然后勾选本页的标尺确认。")
            if not self.image_section_confirmed.get():
                raise ValueError("真实图像尚未确认 TEM 切面位置与高度参考。请填写本页参数并勾选确认。")
            for name in ("sem", "tem"):
                legacy = spec["legacy_config"]
                if not legacy.get(name + "_image") or not Path(legacy[name + "_image"]).is_file():
                    raise ValueError(f"找不到 {name.upper()} 图像；请在标注工具保存正确路径后重新加载配置。")
                old = copy.deepcopy(legacy.get("calibration", {}).get(name, {}))
                legacy.setdefault("calibration", {})[name] = {**old, "confirmed": True,
                    "modeler_confirmation": {"method": "explicit_user_confirmation_of_current_form", "nm_per_pixel": copy.deepcopy(legacy[name + "_nm_per_pixel"])}}
        # Material choice is explicit and captured before the worker starts.
        # A demo must not accidentally borrow same-named project materials.
        materials = copy.deepcopy(self.image_materials)
        if spec.get("materials"):
            materials = json.loads(Path(spec["materials"]).read_text(encoding="utf-8-sig"))
        if materials is None:
            if real_images:
                raise ValueError("真实图像重建需要材料表。请选择材料 JSON，或点击“明确使用当前工程材料”。")
            materials = service.demo_materials()
        validate_library(materials)
        missing = [f"标签 {label}: {name or '未填写'}" for label, name in spec["material_map"].items() if name not in materials["materials"]]
        if missing:
            raise ValueError("所选材料表中没有对应材料：" + "；".join(missing) + "。请核对材料标签名称。")
        output = self.output_folder("reconstruction")
        revision = self._image_revision
        def task():
            result = service.reconstruct(spec, output, materials=materials)
            return {**result, "origin": "image", "image_revision": revision}
        self.work("正在根据图像与标注重建并离散材料标签…", task, self.accept_build)

    def load_model_dialog(self):
        path = filedialog.askopenfilename(title="选择 structure.json / geometry.json / H5 / NPZ", filetypes=[("Geometry", "*.json *.h5 *.hdf5 *.npz")], parent=self.winfo_toplevel())
        if not path:
            return
        # The imported artifact and its adjacent material snapshot are one
        # source. Existing project names must not silently override that data.
        materials = None
        is_bundle = Path(path).name == "structure.json"
        if Path(path).suffix.lower() == ".json":
            metadata = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            is_bundle = isinstance(metadata, dict) and metadata.get("format") == "marksim-structure-bundle"
        if not is_bundle and not (Path(path).parent / "materials.json").exists():
            library = filedialog.askopenfilename(title="几何没有光学材料表：选择 materials.json", filetypes=[("Material library", "*.json")], parent=self.winfo_toplevel())
            if not library:
                return
            materials = library
        output = self.output_folder("imported_geometry")
        self.work("正在校验几何和材料，并创建预览…", lambda: self.load_model_now(path, materials, output, accept=False), self.accept_build)

    def load_model_now(self, path, materials=None, output=None, *, accept=True):
        from .. import service
        model = service.load_model(path, materials)
        output = output or self.output_folder("imported_geometry")
        previews = service.preview_model(model, output)
        result = {"model": model, "output_dir": str(output), "previews": previews, "origin": "import"}
        if accept:
            self.accept_build(result)
        return result

    def voxelize_async(self):
        from .. import service
        if self.model is None:
            raise ValueError("先生成或加载当前结构")
        if self.model["geometry"]["representation"] == "voxel_labels":
            raise ValueError("当前结构已经是材料体素，可以直接预览或导出。此操作只将参数几何转成体素，不会重新缩放已有标签。")
        spacing = self.voxel_spacing()
        model = copy.deepcopy(self.model)
        output = self.output_folder("voxels")
        self.work("正在将真实参数几何转换为材料标签体素…",
                  lambda: {"model": service.voxelize(model, spacing, output), "output_dir": str(output), "origin": "voxel"}, self.accept_build)

    def create_export_page(self, page):
        box = card(page, "导出范围 · 不会添加光源、边界或监视器")
        self.export_mode = tk.StringVar(value="single")
        ttk.Radiobutton(box, text="当前单元（保留母周期与已有模型范围）", variable=self.export_mode, value="single").pack(anchor="w", pady=4)
        ttk.Radiobutton(box, text="有限阵列（显式重复，保留端部，不自动设周期边界）", variable=self.export_mode, value="finite").pack(anchor="w", pady=4)
        self.export_vars = {key: tk.StringVar(value=value) for key, value in {"nx": "3", "ny": "1", "length": "", "wave_lo": "", "wave_hi": "", "max_triangles": "2000000"}.items()}
        self.export_entries = {}
        form = ttk.Frame(box)
        form.pack(fill="x")
        for i, (key, label) in enumerate((("nx", "x 重复数"), ("ny", "y 重复数"), ("length", "单条纹长度 µm（空=保留）"))):
            self.export_entries[key] = field(form, 0, i, label, self.export_vars[key])
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.button(row, "预览本次导出范围", self.preview_export_async)
        hint(box, "有限阵列不覆盖原工程。条纹长度只适用于经检查的沿 y 均匀矩形；任意三维形状和体素不会被拉伸。")
        box = card(page, "输出文件")
        self.export_bundle = tk.BooleanVar(value=True)
        self.export_lsf = tk.BooleanVar(value=True)
        self.export_stl = tk.BooleanVar(value=True)
        for label, var in (("自家结构包：structure.json + geometry.json + materials.json（体素另含 H5）", self.export_bundle),
                           ("Lumerical 2023 R2.1：main.lsf 形状与材料创建代码", self.export_lsf),
                           ("辅助 STL 与单位 / 材料清单（随 Lumerical 导出）", self.export_stl)):
            control = ttk.Checkbutton(box, text=label, variable=var)
            control.pack(anchor="w", pady=4)
            if var is self.export_stl:
                self.export_stl_control = control
        form = ttk.Frame(box)
        form.pack(fill="x")
        for i, (key, label) in enumerate((("wave_lo", "材料导出范围起点 nm（可空）"), ("wave_hi", "终点 nm（可空）"), ("max_triangles", "最大三角面数"))):
            self.export_entries[key] = field(form, 0, i, label, self.export_vars[key])
        self.material_mapping = {}
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.material_mapping_button = self.button(row, "可选：映射 Lumerical 内置材料…", self.edit_material_mapping)
        hint(box, "默认采用工程内的 n、k 数值表。手动映射内置库会改变材料来源，导出报告中明确记录。未知 k 需显式解决后才可生成光学材料脚本。")
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.button(row, "选择新输出文件夹并导出…", self.export_dialog, primary=True)
        self.button(row, "打开最近导出目录", lambda: self.open_path(getattr(self, "last_export", None)))
        self.button(row, "查看完整导出报告", lambda: self.open_path(Path(self.last_export) / "export_report.json" if getattr(self, "last_export", None) else None))
        self.export_report = tk.Text(box, height=11, wrap="word", font=("Consolas", 10), state="disabled")
        self.export_report.pack(fill="x", pady=12)
        hint(box, "目标 2023 R2.1 的运行兼容性需在目标版本检查。LSF 以新结构组导入，不删除已有对象；STL 不自带材料或单位，需配合清单。当前程序不直接写 .fsp；在 Lumerical 运行脚本后另存工程。")
        self.export_mode.trace_add("write", self.refresh_export_controls)
        self.export_lsf.trace_add("write", self.refresh_export_controls)
        self.refresh_export_controls()

    def refresh_export_controls(self, *_):
        finite = self.export_mode.get() == "finite"
        optical = bool(self.export_lsf.get())
        for key, entry in self.export_entries.items():
            active = finite if key in ("nx", "ny", "length") else optical
            entry.state(["!disabled" if active else "disabled"])
        self.export_stl_control.state(["!disabled" if optical else "disabled"])
        self.material_mapping_button.state(["!disabled" if optical else "disabled"])

    def export_settings(self, *, geometry_only=False):
        finite = self.export_mode.get() == "finite"
        optical = self.export_lsf.get() and not geometry_only
        counts = [1, 1]
        length = None
        if finite:
            for i, key in enumerate(("nx", "ny")):
                try:
                    counts[i] = int(self.export_vars[key].get())
                    if not 1 <= counts[i] <= 1000:
                        raise ValueError()
                except ValueError:
                    raise ValueError(f"{'x' if i == 0 else 'y'} 重复数应为 1～1000 的整数。") from None
            if counts[0] * counts[1] > 10000:
                raise ValueError("总重复单元数不能超过 10000，请减小 x 或 y 重复数。")
            text = self.export_vars["length"].get().strip()
            if text:
                length = self._numeric(text, "条纹长度 µm", sizes=((),), positive=True)
        wave, triangles = None, 2000000
        if optical:
            lo, hi = self.export_vars["wave_lo"].get().strip(), self.export_vars["wave_hi"].get().strip()
            if bool(lo) != bool(hi):
                raise ValueError("波长范围必须同时填写起点和终点，或同时留空。")
            if lo:
                wave = (self._numeric(lo, "波长起点 nm", sizes=((),), positive=True), self._numeric(hi, "波长终点 nm", sizes=((),), positive=True))
                if wave[0] >= wave[1]:
                    raise ValueError("波长终点必须大于起点。")
            try:
                triangles = int(self.export_vars["max_triangles"].get())
                if triangles <= 0:
                    raise ValueError()
            except ValueError:
                raise ValueError("最大三角面数应为正整数，例如 2000000。") from None
        if not geometry_only and not self.export_bundle.get() and not self.export_lsf.get():
            raise ValueError("至少勾选“自家结构包”或“Lumerical”。辅助 STL 随 Lumerical 一起导出。")
        return {"nx": counts[0], "ny": counts[1], "length": length, "finite": finite,
                "wave": wave, "triangles": triangles,
                "bundle": self.export_bundle.get(), "lsf": self.export_lsf.get(), "stl": bool(self.export_stl.get() and self.export_lsf.get()),
                "mapping": copy.deepcopy(self.material_mapping)}

    @staticmethod
    def model_for_export(model, settings):
        from .. import service
        return service.repeat_model(model, settings["nx"], settings["ny"], settings["length"], finite=settings["finite"])

    def preview_export_async(self):
        from .. import service
        if self.model is None:
            raise ValueError("先生成或加载结构")
        settings = self.export_settings(geometry_only=True)
        model = copy.deepcopy(self.model)
        output = self.output_folder("export_preview")
        def task():
            exported = self.model_for_export(model, settings)
            previews = service.preview_model(exported, output)
            return {"output": output, "previews": previews}
        def done(result):
            self.status.set("导出范围预览已生成；原始工程模型保持不变。")
            html = list(Path(result["output"]).glob("*.html"))
            self.open_path(html[0] if html else result["output"])
        self.work("正在生成实际导出范围预览…", task, done)

    def edit_material_mapping(self):
        if self.model is None:
            raise ValueError("先生成或加载结构，以确定材料名称")
        window = tk.Toplevel(self)
        window.title("Lumerical 内置材料映射 · 留空采用自己的 n/k")
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        entries = {}
        geometry = self.model["geometry"]
        used = {geometry["background_material"]}
        used.update(s["material"] for s in geometry.get("solids", []))
        if self.model.get("voxels") is not None:
            used.update(self.model["voxels"]["material_map"].values())
        for row, name in enumerate(sorted(used)):
            entries[name] = tk.StringVar(value=self.material_mapping.get(name, ""))
            field(frame, row, 0, name + " → Lumerical 内置材料名", entries[name], width=45)
        def apply():
            self.material_mapping = {name: var.get().strip() for name, var in entries.items() if var.get().strip()}
            window.destroy()
            self.status.set("已设置材料映射；只有填入名称的材料采用内置库。")
        ttk.Button(frame, text="应用映射", command=apply).grid(row=len(entries), column=0, sticky="e", pady=10)
        from .widgets import make_modal
        make_modal(window, self)

    def export_now(self, output, *, model=None, settings=None, accept=True):
        from ..exchange import write_bundle
        from ..lumerical import export_lumerical
        if model is None:
            if self.model is None:
                raise ValueError("当前没有可导出的结构；输入改变后请重新生成")
            model = copy.deepcopy(self.model)
        settings = settings or self.export_settings()
        if not settings["bundle"] and not settings["lsf"]:
            raise ValueError("至少选择一种输出")
        output = Path(output).resolve()
        if output.exists():
            raise FileExistsError("为保留已有结果，请选择尚不存在的新输出文件夹")
        model = self.model_for_export(model, settings)
        output.mkdir(parents=True)
        geometry = model["geometry"]
        used = {geometry["background_material"]}
        used.update(s["material"] for s in geometry.get("solids", []))
        if model.get("voxels") is not None:
            used.update(model["voxels"]["material_map"].values())
        summary = {"representation": geometry["representation"], "materials": sorted(used),
                   "bounds_um": geometry["cell_bounds_um"], "finite_array": geometry.get("finite_array"),
                   "foreground_object_count": len([s for s in geometry.get("solids", []) if s["material"] != geometry["background_material"]]) if model.get("voxels") is None else None,
                   "target_runtime_verified": False}
        report = {"kind": "geometry_only_export", "output_dir": str(output), "settings": settings, "results": {}, "summary": summary}
        try:
            if settings["bundle"]:
                report["results"]["structure"] = write_bundle(model, output / "structure")
            if settings["lsf"]:
                result = export_lumerical(model, output / "lumerical", export_stl=settings["stl"], material_mapping=settings["mapping"],
                                          wavelength_range_nm=settings["wave"], max_triangles=settings["triangles"])
                report["results"]["lumerical"] = {k: v for k, v in result.items() if k != "report"}
                summary["foreground_object_count"] = result["report"]["object_count"]
                summary["stl_files"] = len(result["report"]["stl"])
            report["status"] = "completed_geometry_export"
        except Exception as exc:
            report.update(status="partial_export_failed", error=str(exc))
            (output / "export_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            raise RuntimeError(f"导出未全部完成：{exc}\n已生成文件会保留，请勿把此目录当作完整导出。详情：{output / 'export_report.json'}\n修正后请选择新的输出文件夹。") from exc
        (output / "export_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        if accept:
            self.accept_export(report)
        return report

    def accept_export(self, report):
        self.last_export = Path(report["output_dir"])
        self.export_report.configure(state="normal")
        self.export_report.delete("1.0", "end")
        summary = report.get("summary", {})
        lines = ["几何与材料导出完成", "", "输出目录：" + str(self.last_export)]
        count = summary.get("foreground_object_count")
        if count is not None:
            lines.append(f"前景对象：{count} 个；Lumerical 导出另含有限背景盒。" if report["settings"]["lsf"] else f"前景对象：{count} 个。")
        lines.append("材料：" + "、".join(summary.get("materials", [])))
        if summary.get("bounds_um"):
            lines.append("范围 µm：" + "；".join(f"{axis}={bounds[0]:g}～{bounds[1]:g}" for axis, bounds in zip("xyz", summary["bounds_um"])))
        if summary.get("finite_array"):
            finite = summary["finite_array"]
            lines.append(f"有限阵列：{finite['count_x']} × {finite['count_y']}；未自动添加周期边界。")
        if report["settings"]["bundle"]:
            lines.append("自家软件导入入口：structure / structure.json（请携带整个 structure 文件夹）")
        if report["settings"]["lsf"]:
            lines += ["Lumerical 入口：lumerical / main.lsf", "运行状态：仅生成代码；尚未在 2023 R2.1 执行验证，材料拟合需在目标软件检查。"]
        if summary.get("stl_files"):
            lines.append(f"STL：{summary['stl_files']} 个文件；建议用 lumerical / import_stl.lsf 导入以保留材料与 µm 单位。")
        lines += ["", "完整参数、路径与结果清单：export_report.json"]
        self.export_report.insert("1.0", "\n".join(lines))
        self.export_report.configure(state="disabled")
        self.status.set("几何导出完成：" + str(self.last_export) + "；未运行电磁仿真。")
        self.tabs.select(4)

    def export_dialog(self):
        if self.model is None:
            raise ValueError("先生成或加载当前结构；不能导出旧输入对应的快照")
        settings = self.export_settings()
        output = filedialog.asksaveasfilename(title="新的导出文件夹名称（不能与已有目录重名）", initialfile="Mark_export", parent=self.winfo_toplevel())
        if not output:
            return
        model = copy.deepcopy(self.model)
        self.work("正在导出完整几何、材料表与 Lumerical 脚本…", lambda: self.export_now(output, model=model, settings=settings, accept=False), self.accept_export)
