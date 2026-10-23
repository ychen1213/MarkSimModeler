"""User-facing material workflows against real Tk and actual saved projects."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import tkinter as tk
from tkinter import ttk

import pytest

from markmodel import service
from markmodel.ui import ModelerApp
from markmodel.ui.material_import import read_material_table, material_spec_from_table, columns_from_fields


@pytest.fixture
def material_app(tmp_path):
    root = tk.Tk()
    root.withdraw()
    window = tk.Toplevel(root)
    window.withdraw()
    app = ModelerApp(window)
    app.new_project(tmp_path / "material_project")
    yield app
    root.update_idletasks()
    root.destroy()


@pytest.mark.parametrize("encoding,delimiter,header", [
    ("utf-8-sig", ",", "波长,n,k"),
    ("gb18030", ";", "波长;n;k"),
    ("utf-16", "\t", "波长\tn\tk"),
])
def test_common_windows_tables_preserve_original_source(tmp_path, encoding, delimiter, header):
    path = tmp_path / "光学常数.csv"
    text = header + "\n" + delimiter.join(("0.4", "1.7", "0.03")) + "\n" + delimiter.join(("1.0", "1.5", "0.01")) + "\n"
    raw = text.encode(encoding)
    path.write_bytes(raw)
    table = read_material_table(path)
    spec = material_spec_from_table(table, wavelength_unit="um")
    assert spec["wavelength_nm"] == [400., 1000.]
    assert spec["k"] == [.03, .01]
    assert spec["source"] == str(path.resolve())
    assert spec["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert spec["import"]["original_line_numbers"] == [2, 3]
    assert spec["import"]["header"] == header
    assert path.read_bytes() == raw


def test_unknown_k_is_not_inferred_and_nondefault_columns_are_explicit(tmp_path):
    path = tmp_path / "two.txt"
    path.write_text("1000 1.6\n400 1.8\n400 1.8\n", encoding="utf-8")
    spec = material_spec_from_table(read_material_table(path), wavelength_unit="nm")
    assert spec["k"] is None and spec["assume_lossless"] is False
    assert spec["wavelength_nm"] == [400., 1000.]
    assert spec["import"]["duplicate_identical_rows_removed"] == 1
    path.write_text("1,0.4,0.1,1.7\n2,1.0,0.2,1.5\n", encoding="utf-8")
    table = read_material_table(path)
    with pytest.raises(ValueError, match="明确选择"):
        material_spec_from_table(table, wavelength_unit="um")
    columns = columns_from_fields("2", "4", "3", table.column_count)
    spec = material_spec_from_table(table, wavelength_unit="um", columns=columns, assume_lossless=True)
    assert spec["n"] == [1.7, 1.5] and spec["k"] == [.1, .2]
    assert spec["assume_lossless"] is False


@pytest.mark.parametrize("wave,n,k,match", [
    ("0", "2", "", "波长 列号"), ("1", "1", "", "不同的列"),
    ("1", "2", "3", "k 列号"), ("1.0", "2", "", "波长 列号"),
])
def test_column_errors_explain_recovery(wave, n, k, match):
    with pytest.raises(ValueError, match=match):
        columns_from_fields(wave, n, k, 2)


@pytest.mark.parametrize("text,match", [
    ("400,1.5,\n1000,1.6,0\n", "空列"),
    ("400,1.5,0\n1000,1.6\n", "第 2 行"),
    ("400,nan,0\n1000,1.6,0\n", "NaN"),
    ("400,1.5,0\n1000,broken,0\n", "第 2 行"),
])
def test_malformed_rows_are_never_dropped(tmp_path, text, match):
    path = tmp_path / "bad.csv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        read_material_table(path)


def test_conflicting_duplicate_and_unit_are_reported(tmp_path):
    path = tmp_path / "conflict.txt"
    path.write_text("400 1.5\n400 1.6\n1000 1.7\n", encoding="utf-8")
    table = read_material_table(path)
    with pytest.raises(ValueError, match="冲突"):
        material_spec_from_table(table, wavelength_unit="nm")
    with pytest.raises(ValueError, match="不会猜测"):
        material_spec_from_table(table, wavelength_unit="")


def test_materials_refresh_clears_stale_plot_and_selects_actual_project(material_app, tmp_path):
    app = material_app
    assert app.material_tree.selection()
    assert app.nk_axes[0].lines
    app.material_tree.selection_set("RIDGE_DEMO")
    app.select_material()
    assert list(app.nk_axes[0].lines[0].get_ydata()) == [2., 2., 2.]
    other = service.create_project(tmp_path / "other")
    other["materials"]["materials"]["RIDGE_DEMO"]["n"] = [3.] * 3
    service.save_project(other["folder"], other["process"], other["materials"])
    app.open_project(other["folder"])
    assert list(app.nk_axes[0].lines[0].get_ydata()) == [3.] * 3
    app.data = None
    app.refresh_materials()
    assert not app.material_tree.get_children() and not app.nk_axes[0].lines
    assert "disabled" in app.lossless_check.state()


def test_import_duplicate_bad_input_and_cancel_never_mutate(material_app, tmp_path, monkeypatch):
    app = material_app
    initial = copy.deepcopy(app.data["materials"])
    initial_revision = app._input_revision
    path = tmp_path / "bad.csv"
    path.write_text("400,1.5\n1000,broken\n", encoding="utf-8")
    with pytest.raises(ValueError, match="第 2 行"):
        app.import_material(path, "BAD", "nm")
    with pytest.raises(ValueError, match="已存在"):
        app.add_constant_material("RIDGE_DEMO", 1.5, 0, 400, 1000)
    with pytest.raises(ValueError, match="不能含逗号"):
        app.add_constant_material("Si,O2", 1.5, 0, 400, 1000)
    with pytest.raises(ValueError, match="必须是有限"):
        app.add_constant_material("NAN", "nan", 0, 400, 1000)
    monkeypatch.setattr("markmodel.ui.materials.filedialog.askopenfilename", lambda **kwargs: "")
    app.material_import_dialog()
    assert app.data["materials"] == initial and app._input_revision == initial_revision


def test_delete_guards_process_disabled_steps_and_unapplied_forms(material_app):
    app = material_app
    for name in ("AIR_DEMO", "SUBSTRATE_DEMO", "RIDGE_DEMO"):
        app.material_tree.selection_set(name)
        with pytest.raises(ValueError, match="仍被引用"):
            app.delete_material()
    app.add_constant_material("MY_FILM", 1.8, .01, 400, 1000)
    app.step_tree.selection_set("1")
    app.select_step()
    app.step_vars["material"].set("MY_FILM")
    app.material_tree.selection_set("MY_FILM")
    with pytest.raises(ValueError, match="尚未应用"):
        app.delete_material()
    app.apply_step()
    app.step_enabled.set(False)
    app.apply_step()
    with pytest.raises(ValueError, match="停用"):
        app.delete_material()
    app.add_constant_material("UNUSED", 2.1, 0, 400, 1000)
    app.material_tree.selection_set("UNUSED")
    app.delete_material()
    assert "UNUSED" not in app.data["materials"]["materials"]
    assert "MY_FILM" in app.data["materials"]["materials"]


def test_advanced_edit_is_atomic_and_reaches_saved_geometry(material_app):
    app = material_app
    app.build_now(previews=False)
    original = copy.deepcopy(app.data["materials"]["materials"]["RIDGE_DEMO"])
    revision = app._input_revision
    app.apply_material_edit("RIDGE_DEMO", original)
    assert app.model is not None and app._input_revision == revision
    invalid = copy.deepcopy(original)
    invalid["n"] = [-2.] * 3
    with pytest.raises(ValueError):
        app.apply_material_edit("RIDGE_DEMO", invalid)
    assert app.model is not None and app.data["materials"]["materials"]["RIDGE_DEMO"] == original
    valid = copy.deepcopy(original)
    valid.update(n=[2.7] * 3, k=None, k_status="provided", assume_lossless=False)
    app.apply_material_edit("RIDGE_DEMO", valid)
    assert app.model is None and app.dirty
    assert app.data["materials"]["materials"]["RIDGE_DEMO"]["k_status"] == "unknown"
    built = app.build_now(previews=False)
    saved = service.load_project(app.data["folder"])["materials"]["materials"]["RIDGE_DEMO"]
    assert saved["n"] == [2.7] * 3 and saved["k"] is None
    assert built["model"]["materials"]["materials"]["RIDGE_DEMO"] == saved


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


def test_two_column_import_dialog_is_modal_requires_units_and_cancels_cleanly(material_app, tmp_path, monkeypatch):
    app = material_app
    path = tmp_path / "吸收未知.csv"
    path.write_text("波长,n\n0.4,1.5\n1.0,1.4\n", encoding="utf-8-sig")
    monkeypatch.setattr("markmodel.ui.materials.filedialog.askopenfilename", lambda **kwargs: str(path))
    initial = copy.deepcopy(app.data["materials"])
    dialog = app.material_import_dialog()
    assert app.grab_current() == dialog
    widgets = list(walk(dialog))
    entries = [widget for widget in widgets if isinstance(widget, ttk.Entry) and not isinstance(widget, ttk.Combobox)]
    assert [entry.get() for entry in entries] == [path.stem, "1", "2", ""]
    units = next(widget for widget in widgets if isinstance(widget, ttk.Combobox) and "nm" in widget["values"])
    assert units.get() == ""
    apply = next(widget for widget in widgets if isinstance(widget, ttk.Button) and widget["text"] == "校验并加入工程")
    apply.invoke()
    assert dialog.winfo_exists() and app.data["materials"] == initial
    next(widget for widget in widgets if isinstance(widget, ttk.Button) and widget["text"] == "取消").invoke()
    assert app.grab_current() is None and app.data["materials"] == initial
    dialog = app.material_import_dialog()
    widgets = list(walk(dialog))
    units = next(widget for widget in widgets if isinstance(widget, ttk.Combobox) and "nm" in widget["values"])
    units.set("um")
    next(widget for widget in widgets if isinstance(widget, ttk.Button) and widget["text"] == "校验并加入工程").invoke()
    imported = app.data["materials"]["materials"][path.stem]
    assert imported["wavelength_nm"] == [400., 1000.]
    assert imported["k"] is None and imported["assume_lossless"] is False
    assert app.grab_current() is None
