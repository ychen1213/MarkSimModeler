"""Regression cases reproduced by the independent geometry/dataflow review."""
from __future__ import annotations

import copy
import struct

import numpy as np
import pytest

from markmodel import service
from markmodel.exchange import validate_model
from markmodel.mesh import prism_mesh, solid_meshes, write_binary_stl
from marksim.materials import import_material_txt
from marksim.schemas import read_json, write_json
from marksim.voxel import create_voxel_model, geometry_manifest


def test_build_keeps_explicit_material_edit_without_rereading_original_txt(tmp_path):
    data = service.create_project(tmp_path / "project")
    original = tmp_path / "ridge.txt"
    original.write_text("400 1.7 0\n1000 1.7 0\n", encoding="utf-8")
    imported = import_material_txt(original, wavelength_unit="nm")
    imported["n"] = [2.8, 2.8]  # Equivalent to the GUI's advanced table editor.
    data["materials"]["materials"]["RIDGE_DEMO"] = imported
    # Keep the old import metadata/source: it is provenance, not an instruction
    # to undo an explicit edit every time the geometry is built.
    edited = copy.deepcopy(data["materials"])
    result = service.build_project(data["folder"], materials=edited, previews=False)
    assert result["model"]["materials"]["materials"]["RIDGE_DEMO"]["n"] == [2.8, 2.8]
    assert edited == data["materials"]
    assert original.read_text(encoding="utf-8") == "400 1.7 0\n1000 1.7 0\n"
    service.save_project(data["folder"], data["process"], edited)
    persisted = service.build_project(data["folder"], previews=False)
    assert persisted["model"]["materials"]["materials"]["RIDGE_DEMO"]["n"] == [2.8, 2.8]


@pytest.mark.parametrize("key,value", [
    ("units", "nm"),
    ("axis_order", "zyx"),
    ("origin_kind", "cell_center"),
    ("background_material", "RIDGE_DEMO"),
    ("background_id", 1),
    ("cell_bounds_um", [[10.,20.],[30.,40.],[50.,60.]]),
    ("invariant_y", True),
])
def test_voxel_geometry_manifest_cannot_contradict_payload_semantics(key, value):
    v = create_voxel_model(np.ones((2,2,2), np.uint16), [0.,0.,0.], [.1,.1,.1],
                           {0:"AIR_DEMO", 1:"RIDGE_DEMO"})
    model = {"geometry": geometry_manifest(v, "voxels.h5"), "voxels": v,
             "materials": service.demo_materials()}
    validate_model(model)
    model["geometry"][key] = value
    with pytest.raises(ValueError, match="disagree"):
        validate_model(model)


def test_stripe_length_override_preserves_both_y_faces_far_from_origin(tmp_path):
    data = service.create_project(tmp_path / "project")
    model = service.build_project(data["folder"], previews=False)["model"]
    geometry = model["geometry"]
    geometry["cell_bounds_um"][1] = [1_000_000., 1_000_002.]
    for solid in geometry["solids"]:
        xy = np.asarray(solid["polygon_xy_um"], float)
        xy[:, 1] += 1_000_001.
        solid["polygon_xy_um"] = xy.tolist()
    original = copy.deepcopy(model)
    resized = service.repeat_model(model, 1, 1, 5.)
    assert model == original
    assert resized["geometry"]["cell_bounds_um"][1] == [999_998.5, 1_000_003.5]
    for solid in resized["geometry"]["solids"]:
        y = np.asarray(solid["polygon_xy_um"])[:,1]
        assert sorted(np.unique(y)) == [999_998.5, 1_000_003.5]
        assert len(prism_mesh(solid)["triangles"]) == 12


def test_stl_rejects_noncollapsed_but_numerically_wrong_nanoscale_feature(tmp_path):
    # Float32 does NOT collapse this mesh: 10 nm rounds to 7.8125 nm, a 21.875%
    # dimensional error. Topology-only checks therefore used to accept it.
    solid = {"name":"shifted10nm", "material":"RIDGE_DEMO",
             "polygon_xy_um":[[100_000.,0.],[100_000.01,0.],[100_000.01,1.],[100_000.,1.]],
             "z_min_um":0., "z_max_um":1.}
    mesh = prism_mesh(solid)
    rounded_width = np.ptp(np.asarray(mesh["vertices_um"], np.float32)[:,0])
    assert rounded_width == .0078125
    with pytest.raises(ValueError, match="precision"):
        write_binary_stl(mesh, tmp_path / "distorted.stl")
    assert not (tmp_path / "distorted.stl").exists()


def test_stl_report_describes_actual_float32_file_with_source_error(tmp_path):
    solid = {"name":"ordinary_prism", "material":"RIDGE_DEMO",
             "polygon_xy_um":[[.13,.21],[.45,.21],[.45,.76],[.13,.76]],
             "z_min_um":-.11, "z_max_um":.37}
    path = tmp_path / "safe.stl"
    report = write_binary_stl(prism_mesh(solid), path)
    raw = path.read_bytes()
    count = struct.unpack_from("<I", raw, 80)[0]
    dtype = np.dtype([("normal","<f4",(3,)),("vertices","<f4",(3,3)),("attributes","<u2")])
    xyz = np.frombuffer(raw, dtype=dtype, count=count, offset=84)["vertices"].astype(float)
    actual_volume = np.einsum("ij,ij->i",xyz[:,0],np.cross(xyz[:,1],xyz[:,2])).sum()/6
    assert report["volume_um3"] == pytest.approx(actual_volume, rel=1e-12)
    assert report["source_volume_um3"] == pytest.approx(.32*.55*.48)
    assert 0 < report["max_vertex_rounding_um"] < 1e-7
    assert report["relative_volume_rounding"] < 1e-3


def test_parametric_zero_area_polygon_rejected_before_bundle_or_lsf(tmp_path):
    data = service.create_project(tmp_path / "project")
    model = service.build_project(data["folder"], previews=False)["model"]
    model["geometry"]["solids"] = [model["geometry"]["solids"][0]]
    model["geometry"]["solids"][0]["polygon_xy_um"] = [[-1.,0.],[0.,0.],[1.,0.]]
    with pytest.raises(ValueError, match="Degenerate"):
        validate_model(model)


@pytest.mark.parametrize("external_kind", ["parent_relative", "absolute"])
def test_save_cannot_follow_project_material_path_outside_project(tmp_path, external_kind):
    data = service.create_project(tmp_path / "project")
    external = tmp_path / "external_library.json"
    write_json(external, data["materials"])
    before = external.read_bytes()
    project = read_json(data["folder"] / "project.yaml")
    project["materials"] = "../external_library.json" if external_kind == "parent_relative" else str(external)
    write_json(data["folder"] / "project.yaml", project)
    edited = copy.deepcopy(data["materials"])
    edited["materials"]["RIDGE_DEMO"]["n"] = [4., 4., 4.]
    with pytest.raises(ValueError, match="relative|inside"):
        service.save_project(data["folder"], data["process"], edited)
    assert external.read_bytes() == before
    assert not (data["folder"] / "revisions").exists()


@pytest.mark.parametrize("target", ["process.yaml", "project.yaml"])
def test_project_input_aliases_rejected_before_save(tmp_path, target):
    data = service.create_project(tmp_path / "project")
    path = data["folder"] / "project.yaml"
    project = read_json(path)
    project["materials"] = target
    write_json(path, project)
    before = (data["folder"] / target).read_bytes()
    with pytest.raises(ValueError, match="distinct"):
        service.save_project(data["folder"], data["process"], data["materials"])
    assert (data["folder"] / target).read_bytes() == before


def test_voxel_pinched_vertex_rejected_even_when_every_edge_has_two_faces():
    # The two removed diagonal corner cells meet only at the centre vertex.
    # Every surface edge has two adjacent faces and total volume is six cells,
    # but the central vertex link has TWO cycles: a non-manifold pinch.
    labels = np.ones((2,2,2), np.uint16)
    labels[0,0,0] = labels[1,1,1] = 0
    voxel = create_voxel_model(labels, [0.,0.,0.], [1.,1.,1.],
                               {0:"AIR_DEMO", 1:"RIDGE_DEMO"})
    model = {"geometry": geometry_manifest(voxel, "voxels.h5"), "voxels": voxel,
             "materials": service.demo_materials()}
    validate_model(model)  # Integer labels are valid; only the CAD surface is not.
    with pytest.raises(ValueError, match="pinched vertex"):
        solid_meshes(model)
