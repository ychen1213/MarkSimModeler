"""Numerical/file validation of the exporter, not Lumerical IDE execution."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import struct

import numpy as np
import pytest

from markmodel.lumerical import C0, export_lumerical, sampled_material
from marksim.voxel import create_voxel_model, geometry_manifest


def _table(n=1.5, k=0.0):
    return {"wavelength_nm": [400., 600., 1000.], "n": [n, n+0.1, n+0.2],
            "k": [k, k, k], "k_status": "provided", "assume_lossless": False}


@pytest.fixture
def model():
    # Non-centred geometry tests physical translations and non-convex XY shape.
    geometry = {"schema_version": "1.0", "representation": "parametric_solids", "units": "um",
                "cell_bounds_um": [[0., 5.], [-2., 3.], [-1., 2.]],
                "period_x_um": 5., "period_y_um": None, "invariant_y": False,
                "background_material": "air", "substrate_material": "metal",
                "substrate_extend_bottom": True,
                "solids": [{"name": "条纹一", "material": "metal", "source_step_id": "ridge",
                            "polygon_xy_um": [[1., 0.], [3., 0.], [3., 1.], [2., 1.], [2., 2.], [1., 2.]],
                            "z_min_um": -0.25, "z_max_um": 0.75}]}
    return {"geometry": geometry, "materials": {"schema_version": "1.0", "materials": {
                "air": _table(1., 0.), "metal": _table(0.2, 3.)}}, "voxels": None,
            "source": {"origin": "test geometry; no solver"}}


def _matrix(script, variable):
    rows = re.search(re.escape(variable)+r" = \[\n(.*?)\n\];", script, re.S).group(1)
    # Do not normalize away the LSF error that originally reached the user.
    assert not rows.rstrip().endswith(";"), "LSF matrix must not end with an empty row"
    return np.array([[float(s) for s in row.strip().rstrip(";").split(",")]
                     for row in rows.splitlines()])


def _read_stl(path):
    payload = path.read_bytes()
    count = struct.unpack_from("<I", payload, 80)[0]
    dtype = np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attribute", "<u2")])
    assert len(payload) == 84 + count*50
    return np.frombuffer(payload, dtype=dtype, count=count, offset=84)["vertices"].astype(float)


def test_spectrum_keeps_metal_and_interpolates_nk_without_extrapolation():
    spec = _table(0.2, 3.)
    result = sampled_material("metal", spec, [500., 800.])
    # Source wavelengths -> n,k -> epsilon, not interpolation of epsilon.
    assert result["wavelength_nm"] == [800., 600., 500.]
    np.testing.assert_allclose(result["n"], [.35, .3, .25])
    np.testing.assert_allclose(result["epsilon_real"], np.array([.35, .3, .25])**2-9)
    np.testing.assert_allclose(result["epsilon_imag"], np.array([.35, .3, .25])*6)
    np.testing.assert_allclose(result["frequency_hz"], C0/(np.array([800., 600., 500.])*1e-9))
    assert np.all(np.diff(result["frequency_hz"]) > 0)
    with pytest.raises(ValueError, match="extrapolation"):
        sampled_material("metal", spec, [300., 800.])


def test_missing_k_requires_explicit_boolean_approximation(model, tmp_path):
    table = model["materials"]["materials"]["metal"]
    table["k"] = None
    table["k_status"] = "unknown"
    with pytest.raises(ValueError, match="k unknown"):
        export_lumerical(model, tmp_path/"unknown")
    assert not (tmp_path/"unknown").exists()
    table["assume_lossless"] = "true"
    with pytest.raises(ValueError, match="boolean"):
        export_lumerical(model, tmp_path/"not_boolean")
    table["assume_lossless"] = True
    result = export_lumerical(model, tmp_path/"confirmed", export_stl=False)
    metal = next(m for m in result["report"]["materials"] if m["source_name"] == "metal")
    assert metal["lossless_assumption"] is True
    assert metal["epsilon_imag"] == [0., 0., 0.]
    assert any("assume_lossless=true" in warning for warning in result["report"]["warnings"])


def test_geometry_units_volume_materials_and_portable_script(model, tmp_path):
    original = copy.deepcopy(model)
    result = export_lumerical(model, tmp_path/"export")
    script = (tmp_path/"export/main.lsf").read_text(encoding="utf-8")
    assert model == original  # export never edits input geometry or library
    report = result["report"]
    assert report["object_count"] == 1 and report["target_runtime_verified"] is False
    assert report["objects"][0]["volume_um3"] == pytest.approx(3.)
    xy = _matrix(script, "mm_xy")
    np.testing.assert_allclose(xy, np.asarray(model["geometry"]["solids"][0]["polygon_xy_um"])*1e-6, rtol=0, atol=1e-21)
    assert 'set("z",2.4999999999999999e-07);' in script
    assert 'set("z span",9.9999999999999995e-07);' in script
    forbidden = r"(?m)^\s*(?:deleteall|newproject|clear|switchtolayout|save|addfdtd|addpower|addplane|run)\s*(?:;|\()"
    assert not re.search(forbidden, script)
    assert "C:\\" not in script and "D:\\" not in script
    assert "for (0; mm_collision > 0; 0)" in script
    assert 'getnamednumber("::model::" + mm_group)' in script
    assert 'materialexists(mm_group + "_mat_' in script  # covers orphan material collision
    assert 'set("construction group",0)' in script
    assert 'groupscope(mm_previous_scope)' in script
    assert len(report["stl"]) == 2  # foreground plus background enclosure
    foreground = next(s for s in report["stl"] if s["role"] == "foreground")
    triangles = _read_stl(tmp_path/"export"/foreground["file"])
    signed_volume = np.einsum("ij,ij->i", triangles[:, 0], np.cross(triangles[:, 1], triangles[:, 2])).sum()/6
    assert signed_volume == pytest.approx(3.)
    np.testing.assert_allclose([triangles.min(axis=(0,1)), triangles.max(axis=(0,1))], [[1.,0.,-.25],[3.,2.,.75]])
    assert foreground["lumerical_scaling_factor"] == 1e-6
    for relative, digest in report["files_sha256"].items():
        assert hashlib.sha256((tmp_path/"export"/relative).read_bytes()).hexdigest() == digest
    manifest = json.loads((tmp_path/"export/lumerical_manifest.json").read_text(encoding="utf-8"))
    assert manifest == report


def test_background_priority_and_stl_alternative_preserve_materials(model, tmp_path):
    model["materials"]["materials"]["air"] = _table(1.33)
    report = export_lumerical(model, tmp_path/"bg")["report"]
    assert report["background_object"]["mesh_order"] == 3
    assert report["background_object"]["bounds_um"] == model["geometry"]["cell_bounds_um"]
    assert report["objects"][0]["mesh_order"] == 1
    script = (tmp_path/"bg/import_stl.lsf").read_text(encoding="utf-8")
    assert script.count("stlimport(") == 2
    assert script.count("fileexists(") == 2
    assert ',1e-6,' in script
    assert 'set("mesh order",3);' in script and 'set("mesh order",1);' in script
    assert any("substrate extension" in w for w in report["warnings"])


def test_explicit_builtin_mapping_is_checked_not_claimed_equivalent(model, tmp_path):
    model["materials"]["materials"]["metal"]["k"] = None
    result = export_lumerical(model, tmp_path/"mapped", material_mapping={"metal": "Au (Gold) - Johnson and Christy"})
    mat = next(m for m in result["report"]["materials"] if m["source_name"] == "metal")
    assert mat["mode"] == "builtin" and "not asserted" in mat["optical_equivalence"]
    script = (tmp_path/"mapped/main.lsf").read_text(encoding="utf-8")
    assert 'materialexists("Au (Gold) - Johnson and Christy")' in script
    assert script.index('materialexists("Au') < script.index('addmaterial(')
    with pytest.raises(ValueError, match="unused/unknown"):
        export_lumerical(model, tmp_path/"wrong", material_mapping={"metla": "Au"})
    with pytest.raises(ValueError, match="quotes"):
        export_lumerical(model, tmp_path/"inject", material_mapping={"metal": 'Au"); deleteall; # '})


@pytest.mark.parametrize("band", [[643.,643.], [1000.,400.], [float("nan"),800.], [400.]])
def test_invalid_or_single_frequency_band_rejected(model, tmp_path, band):
    with pytest.raises(ValueError, match="endpoints"):
        export_lumerical(model, tmp_path/"bad", wavelength_range_nm=band)


def test_existing_output_preserved_and_separate_export_names_differ(model, tmp_path):
    a = export_lumerical(model, tmp_path/"a", export_stl=False)
    data = (tmp_path/"a/main.lsf").read_bytes()
    with pytest.raises(FileExistsError, match="preserved"):
        export_lumerical(model, tmp_path/"a")
    assert (tmp_path/"a/main.lsf").read_bytes() == data
    b = export_lumerical(model, tmp_path/"b", export_stl=False)
    assert a["report"]["namespace_prefix"] != b["report"]["namespace_prefix"]
    assert not (tmp_path/"b/stl").exists() and b["stl_import_lsf"] is None


def test_voxel_export_is_closed_three_dimensional_and_conserves_label_volume(model, tmp_path):
    labels = np.zeros((3,3,3), dtype=np.uint16)
    labels[0,0,0] = labels[1,0,0] = labels[1,1,0] = labels[1,1,1] = 1
    v = create_voxel_model(labels, [2.,3.,4.], [.1,.2,.3], {0:"air", 1:"metal"})
    model.update(geometry=geometry_manifest(v, "voxels.h5"), voxels=v)
    result = export_lumerical(model, tmp_path/"voxel")
    report = result["report"]
    assert report["object_count"] == 1 and report["objects"][0]["primitive"] == "addplanarsolid"
    assert report["objects"][0]["volume_um3"] == pytest.approx(4*.1*.2*.3)
    script = (tmp_path/"voxel/main.lsf").read_text(encoding="utf-8")
    vertices = _matrix(script, "mm_vertices")*1e6
    np.testing.assert_allclose([vertices.min(0), vertices.max(0)], [[2.,3.,4.],[2.2,3.4,4.6]])
    assert "addplanarsolid(mm_vertices,mm_facets);" in script
    tri = np.array([[int(x)-1 for x in row.split(",")]
                    for row in re.findall(r"mm_facets\{\d+\}\{1\} = \[([^]]+)\];", script)])
    xyz = vertices[tri]
    signed_volume = np.einsum("ij,ij->i", xyz[:,0],np.cross(xyz[:,1],xyz[:,2])).sum()/6
    assert signed_volume == pytest.approx(4*.1*.2*.3, abs=1e-12)
    assert not np.allclose(vertices[:,1], vertices[0,1])


def test_triangle_limit_stops_export_before_publishing_files(model, tmp_path):
    with pytest.raises(ValueError, match="max_triangles"):
        export_lumerical(model, tmp_path/"too_large", max_triangles=1)
    assert not (tmp_path/"too_large").exists()
