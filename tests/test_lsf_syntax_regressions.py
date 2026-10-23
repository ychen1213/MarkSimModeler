"""Regression checks on exported LSF text, NOT a Lumerical parser/runtime.

The target IDE rejected the former trailing row separator before ``]``.  These
tests inspect complete public exports, including built-in-only material exports
where the first affected matrix is geometry rather than sampled optical data.
They also check numerical payloads so a syntax repair cannot drop the last row.
"""
from __future__ import annotations

from pathlib import Path
import re

import numpy as np
import pytest

from markmodel.lumerical import export_lumerical
from marksim.voxel import create_voxel_model, geometry_manifest


@pytest.fixture
def source_model():
    """Small independent input; never read or change a user's demo project."""
    return {
        "geometry": {
            "schema_version": "1.0", "representation": "parametric_solids",
            "units": "um", "cell_bounds_um": [[0., 5.], [-2., 3.], [-1., 2.]],
            "period_x_um": 5., "period_y_um": None, "invariant_y": False,
            "background_material": "air", "substrate_material": "metal",
            "substrate_extend_bottom": False,
            "solids": [{
                "name": "nonconvex_ridge", "material": "metal", "source_step_id": "ridge",
                "polygon_xy_um": [[1., 0.], [3., 0.], [3., 1.], [2., 1.], [2., 2.], [1., 2.]],
                "z_min_um": -.25, "z_max_um": .75,
            }],
        },
        "materials": {"schema_version": "1.0", "materials": {
            "air": {"wavelength_nm": [400., 600., 1000.], "n": [1., 1., 1.],
                    "k": [0., 0., 0.], "k_status": "provided", "assume_lossless": False},
            "metal": {"wavelength_nm": [400., 600., 1000.], "n": [.2, .3, .4],
                      "k": [3., 2.5, 2.], "k_status": "provided", "assume_lossless": False},
        }},
        "voxels": None,
        "source": {"origin": "syntax regression fixture, no solver results"},
    }


def _literal_arrays(script):
    """Check only the generated numeric-literal subset, not arbitrary LSF."""
    script = re.sub(r"(?m)^\s*#.*$", "", script)
    arrays = []
    assignment = re.compile(r"(\b\w+(?:\{\d+\})*)\s*=\s*\[([^\[\]]*)\]\s*;", re.S)
    for match in assignment.finditer(script):
        name, content = match.groups()
        body = content.strip()
        assert body, f"{name}: empty literal array"
        # Stripping permits the layout newline next to [ and ], but never a
        # blank data row, repeated separator, or a final row separator.
        assert all(line.strip() for line in body.splitlines()), f"{name}: blank data row"
        rows = body.split(";")
        assert all(row.strip() for row in rows), f"{name}: empty row/trailing semicolon"
        values = []
        for row in rows:
            cells = row.strip().split(",")
            assert all(cell.strip() for cell in cells), f"{name}: empty numeric column"
            parsed = []
            for cell in cells:
                cell = cell.strip()
                complex_cell = re.fullmatch(r"\(\s*([^()]+?)\s*\+\s*1i\s*\*\s*([^()]+?)\s*\)", cell)
                parsed.append(complex(float(complex_cell[1]), float(complex_cell[2]))
                              if complex_cell else complex(float(cell)))
            values.append(parsed)
        assert len({len(row) for row in values}) == 1, f"{name}: inconsistent column count"
        arrays.append((name, np.asarray(values, dtype=complex)))
    # Every literal opening bracket emitted by this exporter belongs to one
    # assignment. Do not silently skip a malformed/unmatched array.
    assert len(arrays) == script.count("["), "unrecognized/unclosed literal array"
    return arrays


@pytest.mark.parametrize("representation", ["parametric", "voxel"])
@pytest.mark.parametrize("builtins_only", [False, True], ids=["sampled-nk", "all-builtin"])
def test_exported_arrays_preserve_rows_values_and_geometry(source_model, tmp_path,
                                                          representation, builtins_only):
    model = source_model
    if representation == "voxel":
        labels = np.zeros((3, 3, 3), dtype=np.uint16)
        labels[0, 0, 0] = labels[1, 0, 0] = labels[1, 1, 0] = labels[1, 1, 1] = 1
        voxels = create_voxel_model(labels, [2., 3., 4.], [.1, .2, .3], {0: "air", 1: "metal"})
        model.update(geometry=geometry_manifest(voxels, "voxels.h5"), voxels=voxels)
    mapping = {"air": "Air", "metal": "Au (Gold) - Johnson and Christy"} if builtins_only else None
    result = export_lumerical(model, tmp_path / "export", material_mapping=mapping, export_stl=True)
    main = Path(result["main_lsf"]).read_text(encoding="utf-8")
    stl = Path(result["stl_import_lsf"]).read_text(encoding="utf-8")
    main_arrays, stl_arrays = _literal_arrays(main), _literal_arrays(stl)

    for arrays in (main_arrays, stl_arrays):
        sampled = [data for name, data in arrays if name == "mm_sampled"]
        if builtins_only:
            assert sampled == []
        else:
            assert len(sampled) == 2
            # Expectations come from the independent input spectrum, not the
            # exporter manifest/helper under test. Frequencies must increase.
            for actual, material in zip(sampled, ("air", "metal")):
                table = model["materials"]["materials"][material]
                expected_f = 299792458. / (np.asarray(table["wavelength_nm"])[::-1] * 1e-9)
                expected_eps = (np.asarray(table["n"])[::-1] + 1j * np.asarray(table["k"])[::-1]) ** 2
                assert actual.shape == (3, 2), "last spectral sample must not be dropped"
                np.testing.assert_allclose(actual[:, 0], expected_f, rtol=1e-14)
                np.testing.assert_allclose(actual[:, 1], expected_eps, rtol=1e-14)
                assert np.all(np.diff(actual[:, 0].real) > 0)

    if representation == "parametric":
        polygons = [data for name, data in main_arrays if name == "mm_xy"]
        assert len(polygons) == 1
        expected_xy = np.asarray(model["geometry"]["solids"][0]["polygon_xy_um"]) * 1e-6
        np.testing.assert_allclose(polygons[0], expected_xy, rtol=0, atol=1e-21)
        assert polygons[0].shape == (6, 2), "last polygon vertex must not be dropped"
        assert 'set("z",2.4999999999999999e-07);' in main
        assert 'set("z span",9.9999999999999995e-07);' in main
    else:
        vertex_tables = [data for name, data in main_arrays if name == "mm_vertices"]
        assert len(vertex_tables) == 1
        vertices = vertex_tables[0].real * 1e6
        np.testing.assert_allclose([vertices.min(0), vertices.max(0)],
                                   [[2., 3., 4.], [2.2, 3.4, 4.6]], atol=1e-12)
        facets = [data for name, data in main_arrays if name.startswith("mm_facets{")]
        assert facets and all(data.shape == (1, 3) for data in facets)
        indices = np.concatenate(facets).real.astype(int) - 1
        assert indices.min() >= 0 and indices.max() < len(vertices)
        triangles = vertices[indices]
        volume = np.einsum("ij,ij->i", triangles[:, 0],
                           np.cross(triangles[:, 1], triangles[:, 2])).sum() / 6
        assert volume == pytest.approx(4 * .1 * .2 * .3, abs=1e-12)
        assert "addplanarsolid(mm_vertices,mm_facets);" in main
    assert stl.count("stlimport(") == 2
    assert result["report"]["target_runtime_verified"] is False


def test_material_excerpt_is_documentation_not_an_extra_script(source_model, tmp_path):
    result = export_lumerical(source_model, tmp_path / "export")
    directory = Path(result["directory"])
    assert result["materials_lsf"] is None
    assert Path(result["materials_excerpt"]) == directory / "materials_excerpt.txt"
    assert not (directory / "materials.lsf").exists()
    excerpt = Path(result["materials_excerpt"]).read_text(encoding="utf-8")
    assert "do not execute this excerpt by itself" in excerpt
    assert "mm_group is allocated by main.lsf" in excerpt
    assert "break;" not in excerpt
    arrays = _literal_arrays(excerpt)
    assert len([a for name, a in arrays if name == "mm_sampled"]) == 2
    body = excerpt.splitlines(keepends=True)[2:]
    assert "".join(body) in Path(result["main_lsf"]).read_text(encoding="utf-8")
    assert "materials_excerpt.txt" in (directory / "README.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("malformed", [
    "points = [1,2;3,4;];",       # Former exporter output: empty final row.
    "points = [\n1,2;\n\n3,4\n];",  # Blank physical data row.
    "points = [1,2;;3,4];",       # Empty interior row.
])
def test_literal_checker_rejects_known_bad_row_forms(malformed):
    with pytest.raises(AssertionError, match="empty row|blank data row"):
        _literal_arrays(malformed)
