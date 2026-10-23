"""Actual structure-input integration checks; no fabricated solver output."""
import copy
from pathlib import Path
import shutil
import subprocess
import sys
import tkinter as tk
import pytest
from marksim import service
from marksim.examples import create_ah53_project
from marksim.schemas import read_json, write_json, file_hash, canonical_hash
from marksim.structure_import import read_bundle, input_binding, imported_model


def make_bundle(tmp_path, *, geometry=None):
    example = create_ah53_project(tmp_path/"recipe", previews=False)
    bundle = tmp_path/"bundle"; bundle.mkdir()
    write_json(bundle/"geometry.json", geometry or example["geometry"])
    write_json(bundle/"materials.json", example["materials"])
    manifest = {"schema_version":"1.0","format":"marksim-structure-bundle", "units":"um",
        "coordinate_convention":"x=scan,y=stripe,z=up", "geometry_file":"geometry.json",
        "material_library_file":"materials.json", "files_sha256":{n:file_hash(bundle/n) for n in ("geometry.json","materials.json")}, "provenance":{}}
    write_json(bundle/"structure.json",manifest)
    return bundle, example


def test_actual_prepare_uses_snapshot_not_recipe_or_raw_material(tmp_path, monkeypatch):
    bundle, example = make_bundle(tmp_path)
    result=service.import_structure(bundle,tmp_path/"imported",previews=False)
    folder=Path(result["project_dir"])
    original=read_bundle(bundle)
    for item in example["materials"]["materials"].values():
        Path(item["source"]).write_text("invalid replacement of a historical source",encoding="utf8")
    monkeypatch.setattr(service,"build_geometry",lambda *a,**k: pytest.fail("Imported structure was rebuilt from process"))
    prepared=service.prepare_project(folder,save_artifacts=False)
    assert prepared["task"]["materials"]["RIDGE_DEMO"]["n"] == 2
    assert prepared["task"]["geometry"]["solids"] == original["geometry"]["solids"]
    assert prepared["task"]["geometry_input_snapshot"]["manifest_sha256"]==original["manifest_sha256"]
    assert prepared["estimated_cells"]>0
    data=service.load_project(folder); first=input_binding(data)
    data["simulation"]["lambda_vacuum_nm"]=650
    assert input_binding(data)!=first
    changed=service.prepare_project(folder,simulation=data["simulation"],save_artifacts=False)
    assert changed["task"]["materials"]["RIDGE_DEMO"]["lambda_vacuum_nm"] == 650
    assert canonical_hash(changed["task"]) != canonical_hash(prepared["task"])
    with pytest.raises(ValueError,match="只读"):
        service._set_scan_value(data,"process.steps.0.thickness_um",.4)
    service._set_scan_value(data,"simulation.lambda_vacuum_nm",660)
    wrong=copy.deepcopy(data["materials"]);wrong["materials"]["RIDGE_DEMO"]["n"][0]=3
    with pytest.raises(ValueError,match="只读"):
        service.prepare_project(folder,materials=wrong,save_artifacts=False)
    wrong=copy.deepcopy(data["process"]);wrong["steps"]=[{"operation":"create_film"}]
    with pytest.raises(ValueError,match="只读"):
        service.prepare_project(folder,process=wrong,save_artifacts=False)


def test_tamper_and_new_output_protection(tmp_path):
    bundle,_=make_bundle(tmp_path)
    folder=tmp_path/"imported";service.import_structure(bundle,folder,previews=False)
    with pytest.raises(FileExistsError): service.import_structure(bundle,folder,previews=False)
    geometry=folder/"inputs/structure/geometry.json"
    value=read_json(geometry);value["solids"][0]["z_min_um"]=-3;write_json(geometry,value)
    with pytest.raises(ValueError,match="SHA256"): service.prepare_project(folder,save_artifacts=False)
    manifest=folder/"inputs/structure/structure.json";value=read_json(manifest)
    value["files_sha256"]["geometry.json"]=file_hash(geometry);write_json(manifest,value)
    with pytest.raises(ValueError,match="manifest changed"): service.load_project(folder)


@pytest.mark.parametrize("bad",["../escape.json","C:/outside.json","a/../../escape.json","a\\file.json"])
def test_bundle_path_escape_rejected(tmp_path,bad):
    bundle,_=make_bundle(tmp_path)
    manifest=read_json(bundle/"structure.json");manifest["files_sha256"][bad]="0"*64;write_json(bundle/"structure.json",manifest)
    with pytest.raises(ValueError): read_bundle(bundle)


def test_true_3d_and_finite_arrays_import_but_solver_rejects(tmp_path):
    bundle,_=make_bundle(tmp_path)
    geometry=read_json(bundle/"geometry.json");geometry["invariant_y"]=False;geometry["periodic_x"]=False
    geometry["finite_array"]={"count_x":2,"count_y":1}
    write_json(bundle/"geometry.json",geometry)
    manifest=read_json(bundle/"structure.json");manifest["files_sha256"]["geometry.json"]=file_hash(bundle/"geometry.json");write_json(bundle/"structure.json",manifest)
    folder=tmp_path/"finite";service.import_structure(bundle,folder,previews=False)
    assert imported_model(service.load_project(folder))["geometry"]["finite_array"]["count_x"]==2
    with pytest.raises(ValueError,match="no automatic flattening"):
        service.prepare_project(folder,save_artifacts=False)


def test_copy_is_portable_and_debug_reads_same_snapshot(tmp_path):
    from marksim.debugging import diagnose
    bundle,_=make_bundle(tmp_path)
    first=tmp_path/"first";service.import_structure(bundle,first,previews=False)
    second=tmp_path/"moved";shutil.copytree(first,second)
    a=service.prepare_project(first,save_artifacts=False);b=service.prepare_project(second,save_artifacts=False)
    assert canonical_hash(a["task"])==canonical_hash(b["task"])
    for stage in ("materials","geometry"):
        report=diagnose(stage=stage,project=second,output=tmp_path/"reports")
        assert report["status"]=="passed"
        assert "snapshot" in report["stages"][0]["details"]["scope"]


def test_real_cli_import(tmp_path):
    bundle,_=make_bundle(tmp_path)
    process=subprocess.run([sys.executable,"-m","marksim.cli","import-structure","--source",str(bundle),"--output",str(tmp_path/"cli")],capture_output=True,text=True,encoding="utf8")
    assert process.returncode==0,process.stderr
    assert service.prepare_project(tmp_path/"cli",save_artifacts=False)["task"]["geometry_input_snapshot"]


def test_real_tk_import_save_as_draft_and_guard(tmp_path,monkeypatch):
    from marksim.ui import MarkSimApp
    import marksim.ui.sections.project as project_ui
    bundle,_=make_bundle(tmp_path)
    folder=tmp_path/"gui_import";service.import_structure(bundle,folder,previews=False)
    runtime=tk.Tk();runtime.withdraw()
    app=MarkSimApp(runtime,folder)
    try:
        runtime.update()
        assert app.last_error is None, app.last_error
        assert "只读" in app.structure_mode.get()
        assert app.geometry_folder and app.geometry_image.path
        assert app.data["process"]["steps"]==[]
        with pytest.raises(ValueError,match="只读"): app.add_step()
        with pytest.raises(ValueError,match="只读"): app.edit_mark()
        with pytest.raises(ValueError,match="只读"): app.import_materials()
        first=app.fingerprint()
        app.sim_vars["lambda_vacuum_nm"].set("650")
        assert app.fingerprint()!=first
        app.save_project();app.data=None;app.open_project(folder)
        assert float(app.sim_vars["lambda_vacuum_nm"].get())==650
        parent=tmp_path/"save_as";parent.mkdir()
        monkeypatch.setattr(project_ui.filedialog,"askdirectory",lambda **k:str(parent))
        app.save_as()
        assert app.data["folder"].is_relative_to(parent)
        assert (app.data["folder"]/"inputs/structure/structure.json").is_file()
        assert service.prepare_project(app.data["folder"],save_artifacts=False)["task"]["geometry_input_snapshot"]
        # A geometry/material-changing draft is rejected before it can replace
        # the verified formal project. No loaded result is promoted to current.
        app.save_draft(); draft_path=app.data["folder"]/"ui_draft.json"
        draft=read_json(draft_path);draft["data"]["process"]["steps"]=[{"operation":"create_film"}];write_json(draft_path,draft)
        target=app.data["folder"];app.data=None;app.open_project(target)
        assert app.data["process"]["steps"]==[]
        assert app.scattering is None
        # Switching back to a normal recipe restores editing widgets.
        app.open_project(tmp_path/"recipe")
        assert "可编辑" in app.structure_mode.get()
        app.add_step()
        assert len(app.data["process"]["steps"])==3
        app.process_tree.selection_set("0");app.select_step()
        pending='{"pending_user_text": true}'
        app.step_text.delete("1.0","end");app.step_text.insert("1.0",pending)
        app.save_draft()
        app.open_project(folder)
        app.open_project(tmp_path/"recipe")
        assert app.step_text.get("1.0","end").strip()==pending
    finally:
        app.destroy();runtime.destroy()


def test_actual_voxel_bundle_preview_and_rejection(tmp_path):
    import numpy as np
    from marksim.voxel import create_voxel_model, save_voxels, geometry_manifest
    bundle,_=make_bundle(tmp_path)
    ids=np.zeros((3,2,2),dtype=np.uint16);ids[1,:,:]=1
    voxel=create_voxel_model(ids,[0,0,0],[.1,.1,.1],{0:"AIR_DEMO",1:"RIDGE_DEMO"},background_id=0)
    save_voxels(voxel,bundle/"voxels.h5")
    write_json(bundle/"geometry.json",geometry_manifest(voxel,"voxels.h5"))
    manifest=read_json(bundle/"structure.json")
    manifest["files_sha256"]={name:file_hash(bundle/name) for name in ("geometry.json","materials.json","voxels.h5")}
    write_json(bundle/"structure.json",manifest)
    folder=tmp_path/"voxel_import"
    result=service.import_structure(bundle,folder)
    assert (Path(result["artifact_dir"])/"voxel_sections.png").is_file()
    loaded=imported_model(service.load_project(folder))
    np.testing.assert_array_equal(loaded["voxels"]["material_id"],ids)
    with pytest.raises(ValueError,match="no automatic flattening"):
        service.prepare_project(folder,save_artifacts=False)


def test_unknown_or_removed_input_binding_never_falls_back_to_recipe(tmp_path):
    bundle,_=make_bundle(tmp_path)
    folder=tmp_path/"imported";service.import_structure(bundle,folder,previews=False)
    path=folder/"project.yaml";value=read_json(path)
    value["geometry_input"]["kind"]="unknown";write_json(path,value)
    with pytest.raises(ValueError,match="implicit recipe fallback"): service.load_project(folder)
    value.pop("geometry_input");write_json(path,value)
    with pytest.raises(ValueError,match="descriptive only"): service.load_project(folder)


@pytest.mark.parametrize("key,bad",[("background_id",1),("axis_order","zyx"),("coordinate_convention","different axes")])
def test_voxel_all_canonical_metadata_must_match_hdf5(tmp_path,key,bad):
    import numpy as np
    from marksim.voxel import create_voxel_model,save_voxels,geometry_manifest
    bundle,_=make_bundle(tmp_path)
    model=create_voxel_model(np.zeros((2,2,2),dtype=np.uint16),[0,0,0],[.1,.1,.1],{0:"AIR_DEMO",1:"RIDGE_DEMO"},background_id=0)
    save_voxels(model,bundle/"voxels.h5")
    geometry=geometry_manifest(model,"voxels.h5");geometry[key]=bad
    write_json(bundle/"geometry.json",geometry)
    manifest=read_json(bundle/"structure.json");manifest["files_sha256"]={n:file_hash(bundle/n) for n in ("geometry.json","materials.json","voxels.h5")};write_json(bundle/"structure.json",manifest)
    with pytest.raises(ValueError,match="canonical HDF5"):
        read_bundle(bundle)


def test_degenerate_parametric_polygon_rejected(tmp_path):
    bundle,_=make_bundle(tmp_path)
    geometry=read_json(bundle/"geometry.json");geometry["solids"][0]["polygon_xy_um"]=[[0,0],[1,0],[2,0]]
    write_json(bundle/"geometry.json",geometry)
    manifest=read_json(bundle/"structure.json");manifest["files_sha256"]["geometry.json"]=file_hash(bundle/"geometry.json");write_json(bundle/"structure.json",manifest)
    with pytest.raises(ValueError,match="Degenerate"):
        read_bundle(bundle)
