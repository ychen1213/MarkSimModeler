"""Real Tk/service integration; no mocked geometry, materials, or exports."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tkinter as tk

import gdstk
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from markmodel.ui import ModelerApp
from markmodel.ui.process import inspect_gds_choices
from markmodel import service
from markmodel.exchange import read_bundle


@pytest.fixture(scope="module")
def tk_root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.update_idletasks()
    root.destroy()


@pytest.fixture
def app(tk_root, tmp_path):
    window = tk.Toplevel(tk_root)
    window.withdraw()
    editor = ModelerApp(window)
    editor.new_project(tmp_path / "project")
    yield editor
    tk_root.update_idletasks()
    window.destroy()


def test_real_form_build_and_two_format_export(app, tmp_path):
    assert len(app.tabs.tabs()) == 5
    assert all("光路" not in app.tabs.tab(i, "text") for i in range(5))
    app.add_step()
    app.step_vars["thickness"].set("100")
    app.step_vars["sidewall"].set("50")
    app.apply_step()
    built = app.build_now(previews=False)
    assert len(built["model"]["geometry"]["solids"]) > 4
    assert app.data["process"]["steps"][-1]["sidewall_thickness_um"] == .05
    exported = app.export_now(tmp_path / "export")
    assert exported["status"] == "completed_geometry_export"
    loaded = read_bundle(tmp_path / "export" / "structure")
    assert loaded["geometry"]["solids"] == built["model"]["geometry"]["solids"]
    assert (tmp_path / "export" / "lumerical" / "main.lsf").is_file()
    assert list((tmp_path / "export" / "lumerical").rglob("*.stl"))
    with pytest.raises(FileExistsError):
        app.export_now(tmp_path / "export")


def test_form_change_invalidates_export_and_saves_before_build(app, tmp_path):
    app.build_now(previews=False)
    app.step_tree.selection_set("1")
    app.select_step()
    app.step_vars["thickness"].set("250")
    assert app.model is None
    with pytest.raises(ValueError, match="没有可导出"):
        app.export_now(tmp_path / "old")
    app.build_now(previews=False)
    loaded = service.load_project(app.data["folder"])
    assert loaded["process"]["steps"][1]["thickness_um"] == .25


def test_mark_parameters_replace_only_selected_step(app):
    app.mark_vars["mark_type"].set("AH74")
    app.mark_vars["mother_period_um"].set("14")
    app.mark_vars["cd_nm"].set("900")
    app.apply_mark()
    assert app.data["process"]["mark"]["mark_type"] == "AH74"
    assert app.data["process"]["mark"]["subpitch_um"] == 2
    built = app.build_now(previews=False)
    assert len(built["model"]["geometry"]["solids"]) == 5


def test_real_gds_cell_and_layer_binding(app, tmp_path):
    library = gdstk.Library(unit=1e-6, precision=1e-9)
    child = library.new_cell("CHILD")
    child.add(gdstk.rectangle((-.3, -1), (.3, 1), layer=17, datatype=4))
    top = library.new_cell("TOP")
    top.add(gdstk.Reference(child))
    path = tmp_path / "custom.gds"
    library.write_gds(path)
    assert inspect_gds_choices(path)["cells"]["TOP"] == [(17, 4)]
    app.step_tree.selection_set("1")
    app.select_step()
    with pytest.raises(ValueError, match="不存在"):
        app.bind_gds(path, "TOP", 99, 4)
    app.bind_gds(path, "TOP", 17, 4)
    assert "mark" not in app.data["process"]
    assert path.is_file()
    model = app.build_now(previews=False)["model"]
    assert len(model["geometry"]["solids"]) == 2


def test_missing_k_explicit_policy_and_negative_epsilon_allowed(app, tmp_path):
    txt = tmp_path / "unknown.txt"
    txt.write_text("400,1.7\n1000,1.7\n", encoding="utf-8")
    spec = app.import_material(txt, "UNKNOWN", "nm", columns={"wavelength": 0, "n": 1})
    assert spec["k"] is None and spec["assume_lossless"] is False
    app.material_tree.selection_set("UNKNOWN")
    app.material_lossless.set(True)
    app.set_lossless()
    assert spec["assume_lossless"] is True
    metal = tmp_path / "metal.txt"
    metal.write_text("400,.5,3\n1000,.5,3\n", encoding="utf-8")
    app.import_material(metal, "METAL", "nm")
    app.step_tree.selection_set("1")
    app.select_step()
    app.step_vars["material"].set("METAL")
    app.apply_step()
    app.build_now(previews=False)
    assert any(s["material"] == "METAL" for s in app.model["geometry"]["solids"])
    result = app.export_now(tmp_path / "metal_export")
    assert result["status"] == "completed_geometry_export"


def test_finite_array_export_preserves_original_model(app, tmp_path):
    app.build_now(previews=False)
    before = copy.deepcopy(app.model["geometry"])
    app.export_mode.set("finite")
    app.export_vars["nx"].set("2")
    app.export_vars["ny"].set("1")
    app.export_vars["length"].set("3")
    app.export_lsf.set(False)
    app.export_now(tmp_path / "finite")
    finite = read_bundle(tmp_path / "finite" / "structure")["geometry"]
    assert finite["invariant_y"] is False
    assert len(finite["solids"]) == 2 * len(before["solids"])
    assert finite["cell_bounds_um"][1] == [-1.5, 1.5]
    assert app.model["geometry"] == before


def test_film_modes_and_step_order_are_saved(app):
    app.add_step()
    film_id = app.data["process"]["steps"][-1]["id"]
    app.step_vars["film_mode"].set("填平覆盖（平坦顶面）")
    app.step_vars["thickness"].set("50")
    app.apply_step()
    app.move_step(-1)
    assert app.data["process"]["steps"][1]["id"] == film_id
    app.move_step(1)
    app.step_enabled.set(False)
    app.apply_step()
    app.save()
    step = service.load_project(app.data["folder"])["process"]["steps"][-1]
    assert step["film_mode"] == "planar_fill" and step["enabled"] is False


def test_package_has_no_solver_dependency():
    root = Path(__file__).resolve().parents[1]
    actual = [line.strip().split(">=")[0] for line in (root / "requirements.txt").read_text().splitlines() if line.strip() and not line.startswith("#")]
    assert "fdtd" not in actual and "meep" not in actual


def test_delete_interval_and_disable_patterned_step(app):
    app.add_step()
    app.step_vars["operation"].set("删除材料")
    app.step_vars["material"].set("RIDGE_DEMO")
    app.step_vars["z"].set("0")
    app.step_vars["thickness"].set("100")
    app.apply_step()
    built = app.build_now(previews=False)
    ridges = [s for s in built["model"]["geometry"]["solids"] if s["material"] == "RIDGE_DEMO"]
    assert ridges and all(s["z_min_um"] == .1 for s in ridges)
    app.step_tree.selection_set("1")
    app.select_step()
    app.step_enabled.set(False)
    app.apply_step()
    assert "mark" not in app.data["process"]
    built = app.build_now(previews=False)
    assert all(s["material"] != "RIDGE_DEMO" for s in built["model"]["geometry"]["solids"])


def test_anisotropic_image_calibration_is_not_silently_replaced(app):
    app.image_spec["legacy_config"]["sem_nm_per_pixel"] = [2, 3]
    app.image_spec["spacing_um"] = [.02, .03, .04]
    app.update_image_form()
    actual = app.image_configuration()
    assert actual["legacy_config"]["sem_nm_per_pixel"] == [2, 3]
    assert actual["spacing_um"] == [.02, .03, .04]


def test_one_by_one_finite_export_is_not_periodic(app, tmp_path):
    app.build_now(previews=False)
    app.export_mode.set("finite")
    app.export_vars["nx"].set("1")
    app.export_lsf.set(False)
    app.export_now(tmp_path / "finite_one")
    geometry = read_bundle(tmp_path / "finite_one" / "structure")["geometry"]
    assert geometry["periodic_x"] is False and geometry["invariant_y"] is False


def test_background_result_cannot_replace_newer_input(app):
    import time
    accepted = []
    app.work("real build", lambda: service.build_project(app.data["folder"], previews=False), accepted.append)
    app.changed()
    deadline = time.monotonic() + 20
    while app.busy and time.monotonic() < deadline:
        app.update()
        time.sleep(.02)
    assert not app.busy and not accepted
    assert app.model is None
    assert "运行期间输入已改变" in app.status.get()


def wait_ui(app, timeout=90):
    import time
    deadline = time.monotonic() + timeout
    while app.busy and time.monotonic() < deadline:
        app.update()
        time.sleep(.02)
    assert not app.busy


def test_import_dialog_prefers_own_material_snapshot(app, tmp_path, monkeypatch):
    other = service.create_project(tmp_path / "different_material_project")
    other["materials"]["materials"]["RIDGE_DEMO"]["n"] = [2.7] * 3
    service.save_project(other["folder"], other["process"], other["materials"])
    result = service.build_project(other["folder"], previews=False)
    picks = []
    def pick(**kwargs):
        picks.append(kwargs)
        return str(Path(result["output_dir"]) / "geometry.json")
    monkeypatch.setattr("markmodel.ui.transfer.filedialog.askopenfilename", pick)
    app.load_model_dialog()
    wait_ui(app)
    assert len(picks) == 1
    assert app.data["materials"]["materials"]["RIDGE_DEMO"]["n"] == [2.] * 3
    assert app.model["materials"]["materials"]["RIDGE_DEMO"]["n"] == [2.7] * 3
    assert app.model_origin == "import"


def test_image_config_owns_materials_and_changes_invalidate_only_images(app, tmp_path, monkeypatch):
    app.build_now(previews=False)
    process_model = app.model
    app.image_vars["spacing"].set("0.04")
    assert app.model is process_model
    material_file = tmp_path / "bridge_materials.json"
    library = service.demo_materials()
    library["materials"]["RIDGE_DEMO"]["n"] = [2.9] * 3
    material_file.write_text(json.dumps(library), encoding="utf-8")
    config = copy.deepcopy(app.image_spec)
    config.update(materials=material_file.name, spacing_um=[.04] * 3)
    config_path = tmp_path / "bridge.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr("markmodel.ui.transfer.filedialog.askopenfilename", lambda **kwargs: str(config_path))
    app.load_image_config()
    assert app.image_spec["materials"] == str(material_file.resolve())
    assert app.model is process_model
    app.reconstruct_async()
    wait_ui(app)
    assert app.model_origin == "image"
    assert app.model["materials"]["materials"]["RIDGE_DEMO"]["n"] == [2.9] * 3
    app.image_vars["sem_nm_per_pixel"].set("3.1")
    assert app.model is None
    assert "图像标尺" in app.geometry_note.get()
    # A second real reconstruction uses a captured config. Changing the input
    # before its completion must reject that result as the active model.
    app.reconstruct_async()
    app.image_vars["sem_nm_per_pixel"].set("3.2")
    wait_ui(app)
    assert app.model is None
    assert "在运行中已改变" in app.status.get()
