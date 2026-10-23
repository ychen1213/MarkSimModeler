"""User-workflow regressions: real Tk drafts, GDS masks and generated solids."""
from __future__ import annotations

import copy
from pathlib import Path
import tkinter as tk

import gdstk
import numpy as np
import pytest

from markmodel.ui import ModelerApp
from markmodel import service
from markmodel.exchange import read_bundle


@pytest.fixture(scope="module")
def tk_root():
    window = tk.Tk()
    window.withdraw()
    yield window
    window.destroy()


@pytest.fixture
def editor(tmp_path, tk_root):
    window = tk.Toplevel(tk_root)
    window.geometry("1320x900")
    window.withdraw()
    app = ModelerApp(window)
    app.new_project(tmp_path / "project")
    yield app
    window.update_idletasks()
    window.destroy()


def select(app, index):
    app.step_tree.selection_set(str(index))
    app.select_step()


def test_pending_mark_cd_invalidates_snapshot_and_build_commits_real_mask(editor, tmp_path):
    editor.build_now(previews=False)
    editor.mark_vars["cd_nm"].set("1200")
    assert editor.mark_dirty and editor.model is None
    assert "待应用" in editor.mark_note.get()
    with pytest.raises(ValueError, match="没有可导出"):
        editor.export_now(tmp_path / "stale")
    built = editor.build_now(previews=False)["model"]
    ridge = [s for s in built["geometry"]["solids"] if s["material"] == "RIDGE_DEMO"]
    assert len(ridge) == 3
    assert all(np.ptp(np.array(s["polygon_xy_um"])[:, 0]) == pytest.approx(1.2) for s in ridge)
    assert not editor.mark_dirty
    editor.export_lsf.set(False)
    editor.export_now(tmp_path / "current")
    exported = read_bundle(tmp_path / "current" / "structure")
    assert exported["geometry"]["mark"]["cd_um"] == 1.2


def test_step_apply_keeps_pending_mark_and_distinct_process_offsets(editor):
    select(editor, 1)
    editor.mark_vars["cd_nm"].set("1100")
    editor.step_vars["thickness"].set("250")
    editor.step_vars["cd_bias"].set("100")
    editor.step_vars["dx"].set("40")
    editor.step_vars["name"].set("我的氧化物条纹")
    editor.apply_step()
    assert editor.mark_vars["cd_nm"].get() == "1100" and editor.mark_dirty
    built = editor.build_now(previews=False)["model"]["geometry"]
    step = editor.data["process"]["steps"][1]
    assert step["thickness_um"] == .25
    assert step["cd_bias_um"] == .1 and step["delta_x_um"] == .04
    assert step["name"] == "我的氧化物条纹"
    ridges = [s for s in built["solids"] if s["material"] == "RIDGE_DEMO"]
    assert all(s["z_max_um"] == .25 for s in ridges)
    assert all(np.ptp(np.array(s["polygon_xy_um"])[:, 0]) == pytest.approx(1.2) for s in ridges)


def test_mark_and_step_forms_committed_together_without_erasure(editor):
    select(editor, 1)
    editor.step_vars["thickness"].set("220")
    editor.mark_vars["mother_period_um"].set("10")
    editor.mark_vars["cd_nm"].set("900")
    editor.save()
    persisted = service.load_project(editor.data["folder"])
    assert persisted["process"]["mark"]["cd_um"] == .9
    assert persisted["process"]["period_x_um"] == 10
    assert persisted["process"]["steps"][1]["thickness_um"] == .22


@pytest.mark.parametrize("field,bad", [("cd_nm", "nan"), ("height_nm", ""), ("mother_period_um", "0"), ("cd_nm", "3200")])
def test_invalid_mark_form_blocks_save_without_changing_disk(editor, field, bad):
    path = editor.data["folder"] / "process.yaml"
    before = path.read_bytes()
    editor.mark_vars[field].set(bad)
    with pytest.raises(ValueError):
        editor.save()
    assert path.read_bytes() == before and editor.mark_dirty
    assert "尺寸待修正" in editor.mark_note.get()


def test_switching_steps_commits_current_form_but_blocks_invalid_values(editor):
    select(editor, 1)
    editor.step_vars["thickness"].set("260")
    select(editor, 0)
    assert editor.data["process"]["steps"][1]["thickness_um"] == .26
    assert editor.step_index == 0
    editor.step_vars["thickness"].set("oops")
    select(editor, 1)
    assert editor.step_index == 0 and editor.step_tree.selection() == ("0",)
    assert "请填写数字" in editor.status.get()
    assert editor.step_vars["thickness"].get() == "oops"


def test_operation_change_cleans_stale_negative_mask_and_coordinates(editor):
    select(editor, 1)
    editor.data["process"]["steps"][1]["mask_polarity"] = "negative"
    editor.step_vars["operation"].set("垂直刻蚀")
    editor.step_vars["z"].set("0")
    editor.step_vars["etch_depth"].set("100")
    editor.apply_step()
    assert "z_min_um" not in editor.data["process"]["steps"][1]
    editor.step_vars["operation"].set("贴合表面薄膜")
    editor.step_vars["film_mode"].set("指定绝对高度（高级）")
    editor.step_vars["z"].set("0")
    editor.step_vars["thickness"].set("100")
    editor.apply_step()
    step = editor.data["process"]["steps"][1]
    assert not {"mask", "mask_polarity", "etch_depth_um", "start_z_um"} & step.keys()
    built = editor.build_now(previews=False)["model"]["geometry"]
    films = [s for s in built["solids"] if s["material"] == "RIDGE_DEMO"]
    assert len(films) == 1 and films[0]["z_max_um"] == .1
    assert np.ptp(np.array(films[0]["polygon_xy_um"])[:, 0]) == 16


def test_delete_selects_neighbor_and_empty_table_disables_editor(editor):
    select(editor, 1)
    editor.delete_step()
    assert editor.step_index == 0
    assert editor.step_vars["name"].get() == editor.data["process"]["steps"][0]["name"]
    assert editor.step_actions["GDS"].instate(["disabled"])
    editor.delete_step()
    assert editor.step_index is None and not editor.step_vars["name"].get()
    assert all(control.instate(["disabled"]) for control in editor.step_controls.values())
    assert editor.step_actions["上移"].instate(["disabled"])
    assert editor.step_actions["下移"].instate(["disabled"])


def test_inapplicable_fields_and_move_buttons_follow_selection(editor):
    select(editor, 0)
    assert editor.step_actions["上移"].instate(["disabled"])
    assert editor.step_controls["sidewall"].instate(["disabled"])
    editor.add_step()
    assert editor.step_actions["下移"].instate(["disabled"])
    assert editor.step_controls["z"].instate(["disabled"])
    assert not editor.step_controls["sidewall"].instate(["disabled"])
    editor.step_vars["film_mode"].set("填平覆盖（平坦顶面）")
    assert editor.step_controls["sidewall"].instate(["disabled"])
    editor.step_vars["sidewall"].set("not used")
    editor.apply_step()
    assert "sidewall_thickness_um" not in editor.data["process"]["steps"][-1]
    editor.move_step(-1)
    assert editor.step_index == 1 and not editor.step_actions["下移"].instate(["disabled"])


def test_custom_gds_cannot_be_overwritten_by_pending_template(editor, tmp_path):
    lib = gdstk.Library()
    lib.new_cell("actual").add(gdstk.rectangle((-.25, -1), (.25, 1), layer=9, datatype=2))
    source = tmp_path / "custom.gds"
    lib.write_gds(source)
    select(editor, 1)
    editor.mark_vars["cd_nm"].set("1000")
    before = copy.deepcopy(editor.data["process"])
    with pytest.raises(ValueError, match="待应用 Mark"):
        editor.bind_gds(source, "actual", 9, 2)
    assert editor.data["process"] == before
    editor.apply_mark()
    with pytest.raises(ValueError, match="整数"):
        editor.bind_gds(source, "actual", 9.2, 2)
    editor.bind_gds(source, "actual", 9, 2)
    built = editor.build_now(previews=False)["model"]["geometry"]
    assert "mark" not in built
    assert np.ptp(np.array(built["solids"][-1]["polygon_xy_um"])[:, 0]) == .5


def test_new_project_does_not_inherit_previous_unapplied_mark(editor, tmp_path):
    editor.mark_vars["cd_nm"].set("1300")
    editor.new_project(tmp_path / "other", "AH74")
    assert not editor.mark_dirty
    assert editor.mark_vars["mark_type"].get() == "AH74"
    assert float(editor.mark_vars["cd_nm"].get()) == pytest.approx(8000 / 7, rel=1e-5)


def test_chinese_comma_targets_and_advanced_ids_are_validated(editor):
    editor.add_step()
    editor.step_vars["operation"].set("删除材料")
    editor.step_vars["material"].set("RIDGE_DEMO，SUBSTRATE_DEMO，RIDGE_DEMO")
    editor.step_vars["thickness"].set("100")
    editor.apply_step()
    assert editor.data["process"]["steps"][-1]["target_materials"] == ["RIDGE_DEMO", "SUBSTRATE_DEMO"]
    with pytest.raises(ValueError, match="不能重复"):
        editor._validate_steps([{"id": "same", "operation": "add"}] * 2)

