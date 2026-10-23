"""User-facing transfer regressions, using real Tk, GDS and export services.

Only file pickers/desktop opening are replaced. Geometry, material tables,
voxel conversion, reconstruction and output writers remain real operations.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import time
import tkinter as tk

import numpy as np
from PIL import Image
import pytest

from markmodel import service
from markmodel.exchange import read_bundle
from markmodel.ui import ModelerApp
from marksim.voxel import create_voxel_model, geometry_manifest


@pytest.fixture(scope="module")
def tk_root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(tk_root, tmp_path):
    window = tk.Toplevel(tk_root)
    window.withdraw()
    editor = ModelerApp(window)
    editor.new_project(tmp_path / "project")
    yield editor
    window.destroy()


def wait_ui(app, timeout=90):
    deadline = time.monotonic() + timeout
    while app.busy and time.monotonic() < deadline:
        app.update()
        time.sleep(.02)
    assert not app.busy


def load_config(app, value, path, monkeypatch):
    path.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr("markmodel.ui.transfer.filedialog.askopenfilename", lambda **kwargs: str(path))
    app.load_image_config()


def test_raw_annotation_does_not_inherit_demo_placement_or_materials(app, tmp_path, monkeypatch):
    raw = {"generate_demo": False, "sem_image": "sem.png", "tem_image": "tem.png",
           "sem_nm_per_pixel": 1.4, "tem_nm_per_pixel": [1.3, 1.5]}
    load_config(app, raw, tmp_path / "annotation.json", monkeypatch)
    assert app.image_spec["legacy_config"]["sem_image"] == str((tmp_path / "sem.png").resolve())
    assert app.image_spec["tem_section"]["origin_phys_um"] == [0, 0, 0]
    assert app.image_spec["tem_section"]["placement_status"] == "user_input_required"
    assert not app.image_section_confirmed.get()
    assert "registration" not in app.image_spec and "materials" not in app.image_spec
    assert app.image_spec["material_map"] == {"0": "", "1": "", "2": ""}
    with pytest.raises(ValueError, match="标尺尚未确认"):
        app.reconstruct_async()
    assert not app.busy


def test_invalid_config_preserves_previous_inputs_and_model(app, tmp_path, monkeypatch):
    app.build_now(previews=False)
    current_model = app.model
    before = copy.deepcopy(app.image_spec)
    revision = app._image_revision
    bad = copy.deepcopy(before)
    bad["spacing_um"] = [0, .02, .02]
    with pytest.raises(ValueError, match="体素尺寸"):
        load_config(app, bad, tmp_path / "invalid.json", monkeypatch)
    assert app.image_spec == before and app.model is current_model
    assert app._image_revision == revision


def test_scalar_spacing_and_xy_dictionary_have_unambiguous_axes(app, tmp_path, monkeypatch):
    spec = copy.deepcopy(app.image_spec)
    spec["spacing_um"] = .04
    spec["legacy_config"]["sem_nm_per_pixel"] = {"y": 3, "x": 2}
    load_config(app, spec, tmp_path / "scalar_spacing.json", monkeypatch)
    actual = app.image_configuration()
    assert actual["spacing_um"] == [.04, .04, .04]
    assert actual["legacy_config"]["sem_nm_per_pixel"] == [2., 3.]
    app.image_vars["spacing"].set("true")
    with pytest.raises(ValueError, match="体素尺寸"):
        app.image_configuration()


def test_extra_material_labels_are_rejected_before_background_work(app, tmp_path, monkeypatch):
    spec = copy.deepcopy(app.image_spec)
    spec["material_map"]["3"] = "EXTRA"
    with pytest.raises(ValueError, match="只支持标签"):
        load_config(app, spec, tmp_path / "extra_labels.json", monkeypatch)
    assert not app.busy


def test_real_scale_edit_revokes_confirmation_and_material_source_is_explicit(app, tmp_path, monkeypatch):
    for name in ("sem", "tem"):
        Image.new("L", (4, 4), 100).save(tmp_path / f"{name}.png")
    spec = copy.deepcopy(app.image_spec)
    spec["legacy_config"].update(generate_demo=False, sem_image="sem.png", tem_image="tem.png",
                                calibration={"sem": {"confirmed": True}, "tem": {"confirmed": True}})
    spec["tem_section"]["placement_status"] = "user_confirmed"
    load_config(app, spec, tmp_path / "real.json", monkeypatch)
    assert app.image_calibration_confirmed.get()
    app.image_vars["sem_nm_per_pixel"].set("2.5")
    assert not app.image_calibration_confirmed.get()
    with pytest.raises(ValueError, match="标尺尚未确认"):
        app.reconstruct_async()
    app.image_calibration_confirmed.set(True)
    with pytest.raises(ValueError, match="真实图像重建需要材料表"):
        app.reconstruct_async()
    assert not app.busy
    app.use_project_image_materials()
    snapshot = copy.deepcopy(app.image_materials)
    app.data["materials"]["materials"]["RIDGE_DEMO"]["n"] = [8, 8, 8]
    assert app.image_materials == snapshot


def test_demo_reconstruction_uses_own_materials_and_missing_labels_fail_early(app):
    app.data["materials"]["materials"]["RIDGE_DEMO"]["n"] = [2.9] * 3
    app.image_vars["ridge"].set("MISSING")
    with pytest.raises(ValueError, match="标签 2: MISSING"):
        app.reconstruct_async()
    assert not app.busy
    app.image_vars["ridge"].set("RIDGE_DEMO")
    app.image_vars["spacing"].set("0.08")
    app.reconstruct_async()
    wait_ui(app)
    assert app.model_origin == "image"
    assert app.model["materials"]["materials"]["RIDGE_DEMO"]["n"] == [2.] * 3
    assert app.model["voxels"]["provenance"]["generation_method"] == "synthetic_image_measurement"


def test_bundle_only_ignores_disabled_lumerical_inputs_and_has_readable_report(app, tmp_path):
    app.build_now(previews=False)
    app.export_vars["wave_lo"].set("not-a-wavelength")
    app.export_vars["max_triangles"].set("not-an-integer")
    app.export_lsf.set(False)
    assert app.export_stl_control.instate(["disabled"])
    assert app.export_entries["wave_lo"].instate(["disabled"])
    result = app.export_now(tmp_path / "bundle_only")
    assert result["settings"]["stl"] is False
    loaded = read_bundle(tmp_path / "bundle_only" / "structure")
    assert loaded["geometry"] == app.model["geometry"]
    shown = app.export_report.get("1.0", "end")
    assert "自家软件导入入口" in shown and "前景对象" in shown
    assert '"results"' not in shown
    assert (tmp_path / "bundle_only" / "export_report.json").is_file()


def test_no_format_fails_before_save_dialog(app, tmp_path, monkeypatch):
    app.build_now(previews=False)
    app.export_bundle.set(False)
    app.export_lsf.set(False)
    def forbidden(**kwargs):
        pytest.fail("An invalid selection must be reported before asking for an output path")
    monkeypatch.setattr("markmodel.ui.transfer.filedialog.asksaveasfilename", forbidden)
    with pytest.raises(ValueError, match="至少勾选"):
        app.export_dialog()


def test_range_preview_ignores_optical_settings_and_does_not_replace_model(app, monkeypatch):
    app.build_now(previews=False)
    before = copy.deepcopy(app.model)
    app.export_mode.set("finite")
    app.export_vars["nx"].set("2")
    app.export_vars["wave_lo"].set("500")
    app.export_vars["wave_hi"].set("")
    opened = []
    monkeypatch.setattr(app, "open_path", opened.append)
    app.preview_export_async()
    wait_ui(app)
    assert opened and Path(opened[0]).is_file()
    assert app.model == before


@pytest.mark.parametrize("key,value,match", [("nx", "1.5", "x 重复数"), ("ny", "0", "y 重复数"), ("length", "NaN", "条纹长度")])
def test_finite_inputs_have_field_specific_errors(app, key, value, match):
    app.export_mode.set("finite")
    app.export_vars[key].set(value)
    with pytest.raises(ValueError, match=match):
        app.export_settings()


def test_existing_voxels_are_not_silently_resampled(app):
    voxels = create_voxel_model(np.ones((2, 2, 2), dtype=np.uint16), [0, 0, 0], [.1] * 3,
                                {0: "AIR_DEMO", 1: "SUBSTRATE_DEMO"}, background_id=0)
    app.model = {"geometry": geometry_manifest(voxels, "voxels.h5"), "voxels": voxels, "materials": service.demo_materials()}
    with pytest.raises(ValueError, match="已经是材料体素"):
        app.voxelize_async()
    assert not app.busy


def test_material_mapping_is_modal_and_only_lists_used_materials(app):
    app.build_now(previews=False)
    app.model["materials"]["materials"]["UNUSED"] = copy.deepcopy(app.model["materials"]["materials"]["AIR_DEMO"])
    app.edit_material_mapping()
    windows = [widget for widget in app.winfo_children() if isinstance(widget, tk.Toplevel)]
    assert len(windows) == 1 and app.grab_current() is windows[0]
    def labels(widget):
        result = []
        for child in widget.winfo_children():
            try:
                result.append(str(child.cget("text")))
            except tk.TclError:
                pass
            result.extend(labels(child))
        return result
    assert not any("UNUSED" in text for text in labels(windows[0]))
    windows[0].destroy()


def test_partial_export_error_identifies_preserved_files_and_report(app, tmp_path):
    app.build_now(previews=False)
    app.export_vars["wave_lo"].set("1")
    app.export_vars["wave_hi"].set("2")
    output = tmp_path / "partial"
    with pytest.raises(RuntimeError, match="导出未全部完成.*",):
        app.export_now(output)
    report = json.loads((output / "export_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "partial_export_failed"
    assert (output / "structure" / "structure.json").is_file()
    assert not (output / "lumerical" / "main.lsf").exists()


def test_existing_export_is_never_overwritten(app, tmp_path):
    app.build_now(previews=False)
    target = tmp_path / "existing"
    target.mkdir()
    saved = target / "user_data.txt"
    saved.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError, match="保留已有结果"):
        app.export_now(target)
    assert saved.read_text(encoding="utf-8") == "keep"


def test_save_unconfirmed_reconstruction_draft_roundtrips_material_snapshot(app, tmp_path, monkeypatch):
    image_path = tmp_path / "original_sem.png"
    Image.new("L", (3, 3), 123).save(image_path)
    original_bytes = image_path.read_bytes()
    app.image_spec["legacy_config"].update(generate_demo=False, sem_image=str(image_path), tem_image=str(image_path))
    app.image_vars["sem_nm_per_pixel"].set("[1.2, 1.4]")
    app.image_vars["tem_origin"].set("[2, 3, 0]")
    app.image_vars["height_reference"].set("0.15")
    app.use_project_image_materials()
    expected_materials = copy.deepcopy(app.image_materials)
    assert not app.image_calibration_confirmed.get() and not app.image_section_confirmed.get()
    saved = app.save_image_configuration(tmp_path / "saved" / "draft.json")
    config_path = Path(saved["config"])
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert stored["materials"] == "draft.materials.json"
    assert stored["legacy_config"]["calibration"]["sem"]["confirmed"] is False
    assert stored["tem_section"]["placement_status"] == "user_input_required"
    app.reset_image_config()
    app.data["materials"]["materials"]["RIDGE_DEMO"]["n"] = [9, 9, 9]
    monkeypatch.setattr("markmodel.ui.transfer.filedialog.askopenfilename", lambda **kwargs: str(config_path))
    app.load_image_config()
    restored = app.image_configuration()
    assert restored["legacy_config"]["sem_nm_per_pixel"] == [1.2, 1.4]
    assert restored["tem_section"]["origin_phys_um"] == [2, 3, 0]
    assert restored["tem_section"]["height_reference_um"] == .15
    assert not app.image_calibration_confirmed.get() and not app.image_section_confirmed.get()
    assert json.loads(Path(app.image_spec["materials"]).read_text(encoding="utf-8")) == expected_materials
    assert image_path.read_bytes() == original_bytes
    assert not (config_path.parent / image_path.name).exists()
    with pytest.raises(ValueError, match="标尺尚未确认"):
        app.reconstruct_async()


def test_save_confirmed_form_roundtrips_exact_values_and_external_material_reference(app, tmp_path, monkeypatch):
    material_path = tmp_path / "explicit_materials.json"
    material_path.write_text(json.dumps(service.demo_materials()), encoding="utf-8")
    app.image_spec["materials"] = str(material_path)
    app.image_vars["spacing"].set("[0.03, 0.04, 0.05]")
    app.image_vars["tem_origin"].set("[0.4, 0.7, 0]")
    app.image_vars["tem_nm_per_pixel"].set("2.25")
    app.image_calibration_confirmed.set(True)
    app.image_section_confirmed.set(True)
    result = app.save_image_configuration(tmp_path / "confirmed.json")
    assert result["material_snapshot"] is None
    app.reset_image_config()
    monkeypatch.setattr("markmodel.ui.transfer.filedialog.askopenfilename", lambda **kwargs: result["config"])
    app.load_image_config()
    spec = app.image_configuration()
    assert spec["spacing_um"] == [.03, .04, .05]
    assert spec["legacy_config"]["tem_nm_per_pixel"] == 2.25
    assert spec["tem_section"]["origin_phys_um"] == [.4, .7, 0]
    assert app.image_calibration_confirmed.get() and app.image_section_confirmed.get()
    assert app.image_spec["materials"] == str(material_path.resolve())


def test_save_configuration_refuses_both_existing_config_and_material_snapshot(app, tmp_path):
    app.use_project_image_materials()
    config = tmp_path / "draft.json"
    config.write_text("user config", encoding="utf-8")
    with pytest.raises(FileExistsError, match="保留已有文件"):
        app.save_image_configuration(config)
    assert config.read_text(encoding="utf-8") == "user config"
    assert not (tmp_path / "draft.materials.json").exists()
    sibling = tmp_path / "other.materials.json"
    sibling.write_text("user materials", encoding="utf-8")
    with pytest.raises(FileExistsError, match="保留已有文件"):
        app.save_image_configuration(tmp_path / "other.json")
    assert sibling.read_text(encoding="utf-8") == "user materials"
    assert not (tmp_path / "other.json").exists()
