"""Export editable Lumerical geometry without running or configuring a solver.

The portable LSF is the primary output.  Its sampled materials contain the
supplied optical *spectrum*, not a single-frequency conductivity approximation.
STL is auxiliary: its missing units/materials are restored by import_stl.lsf
and the manifest.  All coordinates retain physical x/y/z and are converted from
micrometres to SI exactly once.

Only documented long-standing script primitives are emitted.  Static and mesh
checks are deliberately separate from execution in the target 2023 R2.1 IDE.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import uuid

import numpy as np

from .materials import freeze_library, validate_library
from .mesh import prism_mesh, solid_meshes, validate_mesh, write_binary_stl

C0 = 299792458.0
TARGET_VERSION = "2023 R2.1"
SOURCES = {
    "sampled_material": "https://optics.ansys.com/hc/en-us/articles/360034409654-setmaterial-Script-command",
    "material_fit": "https://optics.ansys.com/hc/en-us/articles/360034915093-Creating-new-sampled-data-materials-in-FDTD",
    "polygon": "https://optics.ansys.com/hc/en-us/articles/360034901493-Polygon-Simulation-Object",
    "planar_solid": "https://optics.ansys.com/hc/en-us/articles/360034924113-addplanarsolid-Script-command",
    "stl": "https://optics.ansys.com/hc/en-us/articles/360034924733-stlimport-Script-command",
}


def _number(value):
    value = float(value)
    if not np.isfinite(value):
        raise ValueError("A nonfinite number cannot be written to Lumerical")
    return format(value, ".17g")


def _matrix_lines(variable, rows):
    """Emit LSF rows with separators BETWEEN rows, never before the closing ].

    A trailing semicolon is an empty final row in LSF, unlike the permissive
    syntax of some other array languages. This applies to optical data and to
    both polygon/planar-solid vertex tables.
    """
    rows = list(rows)
    if not rows:
        raise ValueError("Cannot export an empty Lumerical matrix")
    return [f"{variable} = [", *[
        "  " + ", ".join(row) + (";" if i < len(rows) - 1 else "")
        for i, row in enumerate(rows)
    ], "];"]


def _slug(value):
    """Names in scripts/files are ASCII identifiers; source names stay in JSON."""
    return re.sub(r"[^A-Za-z0-9_]", "_", str(value)).strip("_")[:48] or "object"


def _literal(value):
    # Built-in material names are data, never executable LSF. Avoid depending on
    # escaping rules that differ across old scripting engines.
    if not isinstance(value, str) or not value.strip() or any(
        ord(c) < 32 or c in '\\"\'' for c in value
    ):
        raise ValueError("Lumerical names must be nonempty and cannot contain quotes, backslashes or control characters")
    return '"' + value + '"'


def _json(value):
    def default(item):
        if isinstance(item, Path):
            return str(item)
        if isinstance(item, np.ndarray):
            return item.tolist()
        if isinstance(item, np.generic):
            return item.item()
        raise TypeError(f"Cannot serialize {type(item).__name__}")
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=default) + "\n"


def sampled_material(name, spec, wavelength_range_nm=None):
    """Validate and convert one n/k spectrum, linearly interpolating only n/k.

    A requested band may add two endpoint samples but never extrapolates or
    replaces a table with a constant index. Negative real epsilon is valid.
    Unknown k requires the explicit user-selected lossless approximation.
    """
    validate_library({"schema_version": "1.0", "materials": {name: spec}}, require_known_k=True)
    wave = np.asarray(spec["wavelength_nm"], float)
    n = np.asarray(spec["n"], float)
    assumed = spec.get("k") is None
    k = np.zeros_like(n) if assumed else np.asarray(spec["k"], float)
    original_range = [float(wave[0]), float(wave[-1])]
    if wavelength_range_nm is not None:
        requested = np.asarray(wavelength_range_nm, float)
        if requested.shape != (2,) or not np.isfinite(requested).all() or requested[0] <= 0 or requested[1] <= requested[0]:
            raise ValueError("wavelength_range_nm requires two finite increasing positive endpoints")
        lo, hi = requested
        if lo < wave[0] or hi > wave[-1]:
            raise ValueError(f"{name}: requested wavelength band is outside supplied data; extrapolation disabled")
        selected = np.unique(np.r_[lo, wave[(wave > lo) & (wave < hi)], hi])
        n, k = np.interp(selected, wave, n), np.interp(selected, wave, k)
        wave = selected
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        frequency = C0 / (wave * 1e-9)
        epsilon = (n + 1j * k) ** 2
    if not np.isfinite(frequency).all() or not np.isfinite(epsilon).all():
        raise ValueError(f"{name}: optical table overflows frequency or permittivity")
    order = np.argsort(frequency)
    return {
        "source_name": name, "mode": "sampled_nk", "lossless_assumption": assumed,
        "original_wavelength_range_nm": original_range,
        "wavelength_range_nm": [float(wave[0]), float(wave[-1])],
        "wavelength_nm": wave[order].tolist(), "n": n[order].tolist(), "k": k[order].tolist(),
        "frequency_hz": frequency[order].tolist(), "epsilon_real": epsilon.real[order].tolist(),
        "epsilon_imag": epsilon.imag[order].tolist(), "sample_count": len(wave),
        "source": spec.get("original_source", spec.get("source")),
        "source_sha256": spec.get("source_sha256"),
        "interpolation": "linear in n and k; no extrapolation",
        "fit_status": "requires Lumerical Material Explorer verification; no fit has been performed here",
    }


def _materials(model, mapping, wavelength_range_nm):
    g = model["geometry"]
    if g["representation"] == "parametric_solids":
        used = {s["material"] for s in g["solids"]}
    else:
        voxel = model["voxels"]
        used = {voxel["material_map"][str(int(i))] for i in np.unique(voxel["material_id"])}
    used.add(g["background_material"])
    mapping = dict(mapping or {})
    unknown = set(mapping) - used
    if unknown:
        raise ValueError(f"Material mappings name unused/unknown materials: {sorted(unknown)}")
    library = model["materials"]["materials"]
    result = []
    for i, name in enumerate(sorted(used), 1):
        if name in mapping:
            target = mapping[name]
            if isinstance(target, dict):
                if target.get("kind") != "builtin" or set(target) - {"kind", "name"}:
                    raise ValueError("Material mapping object must be {kind: builtin, name: exact database name}")
                target = target.get("name")
            _literal(target)
            record = {"source_name": name, "mode": "builtin", "builtin_name": target,
                      "optical_equivalence": "not asserted; user explicitly selected Lumerical database material"}
        else:
            if name not in library:
                raise ValueError(f"Missing material table for {name}")
            record = sampled_material(name, library[name], wavelength_range_nm)
        record.update(variable=f"mm_mat_{i:04d}", suffix=f"mat_{i:04d}_{_slug(name)}")
        result.append(record)
    return result


def _preamble(prefix, materials, stl_records=None):
    """Preflight before mutating objects. Every run gets a fresh namespace.

    The material collision test also covers a preceding partially failed run
    whose structure group was never created. LSF uses for(0; condition; 0),
    not Python's while syntax. break terminates a script, not merely a loop.
    """
    lines = [
        "# MarkSimModeler geometry export. Target: Lumerical 2023 R2.1.",
        "# Target IDE execution is NOT validated by Python/static mesh tests.",
        "# Use Layout mode. Existing analysis results are never cleared here.",
        "# Physical x=scan, y=stripe, z=up. LSF coordinates below are metres.",
        'if (layoutmode == 0) { ?"STOP: enter Layout mode explicitly before creating geometry."; break; }',
    ]
    for mat in materials:
        if mat["mode"] == "builtin":
            name = _literal(mat["builtin_name"])
            lines.append(f'if (materialexists({name}) == 0) {{ ?"STOP: a requested built-in material is absent: " + {name}; break; }}')
    if stl_records is not None:
        lines += ["# Set Lumerical's working directory to this export folder before running.",
                  '# The main.lsf alternative embeds geometry and needs no working-directory change.']
        for record in stl_records:
            path = _literal(record["file"])
            lines.append(f'if (fileexists({path}) == 0) {{ ?"STOP: STL file missing; set working directory to export folder: " + {path}; break; }}')
    lines += [f'mm_base = "{prefix}";', "mm_run = 0;", "mm_collision = 1;",
              "for (0; mm_collision > 0; 0) {", "  mm_run = mm_run + 1;",
              '  mm_group = mm_base + "_" + num2str(mm_run);',
              '  mm_collision = getnamednumber("::model::" + mm_group);']
    for mat in materials:
        if mat["mode"] == "sampled_nk":
            lines.append(f'  mm_collision = mm_collision + materialexists(mm_group + "_{mat["suffix"]}");')
    lines += ["}", "mm_previous_scope = groupscope;", 'groupscope("::model");', ""]
    return lines


def _material_lines(materials):
    lines = ["# Materials: frequency [Hz], complex RELATIVE epsilon=(n+1i*k)^2.",
             "# Sampled-data dispersion will be fitted by Lumerical. Inspect the fit before solving."]
    for mat in materials:
        var = mat["variable"]
        if mat["mode"] == "builtin":
            lines += ["# Explicit database mapping: equality to the source n/k is not assumed.",
                      f'{var} = {_literal(mat["builtin_name"])};']
            continue
        lines.append(f'{var} = mm_group + "_{mat["suffix"]}";')
        if mat["lossless_assumption"]:
            lines.append("# User explicitly selected k=0 for missing k; this is an approximation.")
        lines += ['mm_created_material = addmaterial("Sampled data");',
                  f'setmaterial(mm_created_material,"name",{var});']
        lines += _matrix_lines("mm_sampled", (
            [_number(frequency), f"({_number(re_eps)} + 1i*{_number(im_eps)})"]
            for frequency, re_eps, im_eps in zip(mat["frequency_hz"], mat["epsilon_real"], mat["epsilon_imag"])
        ))
        lines += [f'setmaterial({var},"sampled data",mm_sampled);',
                  f'setmaterial({var},"specify fit range",1);',
                  f'setmaterial({var},"wavelength min",{_number(mat["wavelength_range_nm"][0]*1e-9)});',
                  f'setmaterial({var},"wavelength max",{_number(mat["wavelength_range_nm"][1]*1e-9)});', ""]
    return lines


def _group_lines():
    return ["# New group per execution, with its origin at the physical origin.",
            "addstructuregroup;", 'set("name",mm_group);',
            'set("x",0); set("y",0); set("z",0);',
            'set("construction group",0);', ""]


def _object_tail(name, material_var, mesh_order):
    return [f'set("name","{name}");', f'set("material",{material_var});',
            'set("override mesh order from material database",1);',
            f'set("mesh order",{mesh_order});', 'addtogroup("::model::" + mm_group);', ""]


def _polygon_lines(solid, name, material_var):
    xy = np.asarray(solid["polygon_xy_um"], float)
    # A repeated closing point is not a separate polygon vertex. Counterclockwise
    # ordering matches the documented polygon convention.
    if np.array_equal(xy[0], xy[-1]):
        xy = xy[:-1]
    signed = np.sum(xy[:, 0] * np.roll(xy[:, 1], -1) - xy[:, 1] * np.roll(xy[:, 0], -1))
    if signed < 0:
        xy = xy[::-1]
    lines = ["# XY polygon extruded along physical z."]
    lines += _matrix_lines("mm_xy", ([_number(x*1e-6), _number(y*1e-6)] for x, y in xy))
    lines += ["addpoly;", 'set("x",0); set("y",0);', 'set("vertices",mm_xy);',
              f'set("z",{_number((solid["z_min_um"] + solid["z_max_um"])*0.5e-6)});',
              f'set("z span",{_number((solid["z_max_um"] - solid["z_min_um"])*1e-6)});']
    return lines + _object_tail(name, material_var, 1)


def _planar_lines(mesh, name, material_var):
    lines = ["# Closed three-dimensional material surface; facet indices are one-based."]
    lines += _matrix_lines("mm_vertices", ([_number(v*1e-6) for v in point] for point in mesh["vertices_um"]))
    triangles = np.asarray(mesh["triangles"])
    lines += [f"mm_facets = cell({len(triangles)});"]
    for i, tri in enumerate(triangles, 1):
        lines += [f"mm_facets{{{i}}} = cell(1);",
                  f'mm_facets{{{i}}}{{1}} = [{",".join(str(int(v)+1) for v in tri)}];']
    lines += ["addplanarsolid(mm_vertices,mm_facets);", 'set("x",0); set("y",0); set("z",0);']
    return lines + _object_tail(name, material_var, 1)


def _background_solid(geometry):
    (x0, x1), (y0, y1), (z0, z1) = geometry["cell_bounds_um"]
    return {"name": "background_enclosure", "material": geometry["background_material"],
            "polygon_xy_um": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
            "z_min_um": z0, "z_max_um": z1}


def _background_lines(geometry, material_var):
    lines = ["# Finite background enclosure. Foreground order 1 wins over background order 3.",
             "# No claim is made about the medium outside these finite bounds.", "addrect;"]
    for axis, limits in zip("xyz", geometry["cell_bounds_um"]):
        lines += [f'set("{axis}",{_number((limits[0]+limits[1])*0.5e-6)});',
                  f'set("{axis} span",{_number((limits[1]-limits[0])*1e-6)});']
    return lines + _object_tail("background_enclosure", material_var, 3)


def _finish():
    return ['groupscope(mm_previous_scope);', '?"Created geometry group: " + mm_group;',
            '?"Verify dimensions, material fits, external medium and substrate/PML extent before simulation.";',
            "# Save As in the IDE only after reviewing the structure. This script does not save a project."]


def export_lumerical(model, output_dir, *, export_stl=True, material_mapping=None,
                     wavelength_range_nm=None, max_triangles=2_000_000):
    """Write a new export directory, refusing to overwrite any existing files.

    Returns paths plus a JSON-safe report. No Lumerical installation/license is
    needed to export. This function makes no claim that the IDE has executed
    the script or that its dispersive material fits meet any tolerance.
    """
    from .exchange import validate_model
    validate_model(model)
    if type(export_stl) is not bool:
        raise ValueError("export_stl must be a boolean")
    if type(max_triangles) is not int or max_triangles <= 0:
        raise ValueError("max_triangles must be a positive integer")
    if wavelength_range_nm is not None:
        band = np.asarray(wavelength_range_nm, float)
        if band.shape != (2,) or not np.isfinite(band).all() or not 0 < band[0] < band[1]:
            raise ValueError("wavelength_range_nm requires two finite increasing positive endpoints")
    destination = Path(output_dir).resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError(f"Existing export/data preserved; choose an empty new folder: {destination}")
    geometry = model["geometry"]
    materials = _materials(model, material_mapping, wavelength_range_nm)
    variables = {m["source_name"]: m["variable"] for m in materials}
    # Precompute and validate surfaces before creating export files. Parameter
    # polygons need no tessellation for the primary addpoly route.
    meshes = solid_meshes(model, max_triangles=max_triangles) if export_stl or geometry["representation"] == "voxel_labels" else []
    background_mesh = prism_mesh(_background_solid(geometry))
    prefix = "MSM_" + uuid.uuid4().hex[:12]
    material_lines = _material_lines(materials)
    body = _group_lines() + _background_lines(geometry, variables[geometry["background_material"]])
    objects = []
    if geometry["representation"] == "parametric_solids":
        solids = [s for s in geometry["solids"] if s["material"] != geometry["background_material"]]
        for i, solid in enumerate(solids, 1):
            name = f"obj_{i:05d}_{_slug(solid.get('name', 'solid'))}"
            body += _polygon_lines(solid, name, variables[solid["material"]])
            xy = np.asarray(solid["polygon_xy_um"], float)
            volume = abs(np.sum(xy[:, 0]*np.roll(xy[:, 1], -1)-xy[:, 1]*np.roll(xy[:, 0], -1)))/2 * (solid["z_max_um"]-solid["z_min_um"])
            objects.append({"object_name": name, "source_name": solid.get("name"), "material": solid["material"],
                            "role": "foreground", "mesh_order": 1, "primitive": "addpoly",
                            "source_step_id": solid.get("source_step_id"), "volume_um3": float(volume),
                            "bounds_um": [[float(xy[:, 0].min()), float(xy[:, 0].max())],
                                          [float(xy[:, 1].min()), float(xy[:, 1].max())],
                                          [solid["z_min_um"], solid["z_max_um"]]]})
    else:
        for i, mesh in enumerate(meshes, 1):
            name = f"obj_{i:05d}_{_slug(mesh.get('name', 'surface'))}"
            body += _planar_lines(mesh, name, variables[mesh["material"]])
            objects.append({"object_name": name, "source_name": mesh.get("name"), "material": mesh["material"],
                            "role": "foreground", "mesh_order": 1, "primitive": "addplanarsolid",
                            **validate_mesh(mesh)})
    background = {"object_name": "background_enclosure", "material": geometry["background_material"],
                  "role": "background_enclosure", "mesh_order": 3, "primitive": "addrect",
                  **validate_mesh(background_mesh)}
    warnings = [
        "2023 R2.1 target execution has not been verified; Python checks do not validate IDE compatibility.",
        "Material spectra require a Lumerical dispersive fit; inspect Material Explorer over the simulation band.",
        "Background is a finite enclosure with mesh order 3; foreground order 1 overrides it. Outside medium is not configured.",
        "No sources, monitors, solver region, boundaries or project-save command are generated.",
        "Importing into an existing project may overlap its objects; namespace isolation does not remove physical overlaps.",
    ]
    if geometry.get("substrate_extend_bottom"):
        warnings.append("Source requests substrate extension, but exported solids stop at finite cell bounds; extend to the intended PML/domain explicitly in Lumerical.")
    if geometry.get("invariant_y") or geometry.get("period_x_um") or geometry.get("period_y_um"):
        warnings.append("Periodicity and y invariance are provenance only. The exported geometry has finite bounds and does not create boundary conditions.")
    for mat in materials:
        if mat.get("lossless_assumption"):
            warnings.append(f'{mat["source_name"]}: missing k replaced by zero only because assume_lossless=true was explicitly selected.')
        if mat["mode"] == "builtin":
            warnings.append(f'{mat["source_name"]}: explicit built-in mapping to {mat["builtin_name"]}; optical equivalence is not asserted.')
    destination.mkdir(parents=True, exist_ok=True)
    stl_records = []
    if export_stl:
        for mesh, obj in [*zip(meshes, objects), (background_mesh, background)]:
            relative = f'stl/{obj["object_name"]}.stl'
            result = write_binary_stl(mesh, destination / relative)
            vertices = np.asarray(mesh["vertices_um"], np.float32).astype(float)
            triangles = np.asarray(mesh["triangles"])
            edge = np.linalg.norm(vertices[triangles[:, 0]]-vertices[triangles[:, 1]], axis=1).min()
            record = {**result, "file": relative, "object_name": obj["object_name"], "material": obj["material"],
                      "role": obj["role"], "mesh_order": obj["mesh_order"],
                      "vertex_radius_m": float(min(1e-12, edge*1e-9))}
            stl_records.append(record)
    main = _preamble(prefix, materials) + material_lines + body + _finish()
    (destination / "main.lsf").write_text("\n".join(main) + "\n", encoding="utf-8")
    (destination / "materials_excerpt.txt").write_text(
        "# Inspection excerpt. main.lsf embeds this section; do not execute this excerpt by itself.\n"
        "# mm_group is allocated by main.lsf after checking for collisions.\n"
        + "\n".join(material_lines) + "\n", encoding="utf-8")
    if export_stl:
        lines = _preamble(prefix, materials, stl_records) + material_lines + _group_lines()
        for record in stl_records:
            lines += [f'stlimport("{record["file"]}",1e-6,{_number(record["vertex_radius_m"])},0);',
                      'set("x",0); set("y",0); set("z",0);']
            lines += _object_tail(record["object_name"], variables[record["material"]], record["mesh_order"])
        lines += _finish()
        (destination / "import_stl.lsf").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (destination / "materials_snapshot.json").write_text(_json(freeze_library(model["materials"])), encoding="utf-8")
    for mat in materials:
        if mat["mode"] != "sampled_nk":
            continue
        relative = f'materials/{mat["suffix"]}.csv'
        path = destination / relative
        path.parent.mkdir(exist_ok=True)
        data = np.column_stack([mat[key] for key in ("wavelength_nm", "n", "k", "frequency_hz", "epsilon_real", "epsilon_imag")])
        np.savetxt(path, data, delimiter=",", fmt="%.17g", comments="",
                   header="wavelength_nm,n,k,frequency_hz,epsilon_real,epsilon_imag")
        mat["table_file"] = relative
    readme = _export_readme(geometry, objects, warnings, export_stl)
    (destination / "README.txt").write_text(readme, encoding="utf-8")
    report = {
        "schema_version": "1.0", "format": "marksim-lumerical-geometry-export",
        "target_version": TARGET_VERSION, "target_runtime_verified": False,
        "verification_scope": "Python geometry/material validation and generated-file checks only",
        "coordinate_convention": "physical x=scan,y=stripe,z=up; identity axis mapping",
        "input_units": "um", "lsf_units": "m", "stl_units": "um", "stl_scaling_factor": 1e-6,
        "namespace_prefix": prefix, "rerun_policy": "allocate fresh group and local material names; never reuse an existing group/material",
        "source": copy.deepcopy(model.get("source", {})),
        "geometry_representation": geometry["representation"], "cell_bounds_um": geometry["cell_bounds_um"],
        "period_x_um": geometry.get("period_x_um"), "period_y_um": geometry.get("period_y_um"),
        "invariant_y": geometry.get("invariant_y", False),
        "object_count": len(objects), "objects": objects, "background_object": background,
        "materials": materials, "stl": stl_records, "warnings": warnings, "sources": SOURCES,
    }
    report["files_sha256"] = {p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted(destination.rglob("*")) if p.is_file()}
    manifest = destination / "lumerical_manifest.json"
    manifest.write_text(_json(report), encoding="utf-8")
    return {"directory": str(destination), "main_lsf": str(destination / "main.lsf"),
            "materials_excerpt": str(destination / "materials_excerpt.txt"),
            "materials_lsf": None,  # Compatibility key; the excerpt is no longer an executable entry point.
            "manifest": str(manifest),
            "stl_import_lsf": str(destination / "import_stl.lsf") if export_stl else None,
            "report": report}


def _export_readme(geometry, objects, warnings, export_stl):
    text = [
        "MarkSimModeler → Lumerical 几何导出", "",
        "目标版本：2023 R2.1。尚未在该版本执行验证，不包含 FDTD 仿真结果。",
        "1. 在 Lumerical 中创建空工程，或先保存当前工程；手动进入 Layout 模式。",
        "2. 用 Script File Editor 打开并运行 main.lsf。脚本自包含，不依赖原电脑路径。",
        "3. 查看新建 MSM_* 结构组，核对坐标、材料和尺寸；在 Material Explorer 检查色散拟合。",
        "4. 自行设置求解区域、背景延伸、边界、光源与监视器，最后在 IDE 中另存为 .fsp。",
        "再次运行会创建新组，不覆盖旧组；若做对比请手动禁用旧组，避免两套结构同时参与仿真。",
        "",
        f"当前含 {len(objects)} 个前景对象，另有有限背景盒；来源 {geometry['representation']}。",
        "背景盒 order=3，前景对象 order=1；背景盒覆盖体积不应与前景体积相加。",
        "物理轴 x/y/z 不变。LSF 用米；STL 数值用微米，导入比例必须为 1e-6。",
        "多边形采用 XY 轮廓沿 z 拉伸；体素采用材料表面三角面片，不自动平滑或压成二维。",
        "只运行 main.lsf 即可同时创建材料和结构，不需要先运行其他材料脚本。",
        "materials_excerpt.txt 是材料段的阅读摘录，不是运行入口。",
        "materials_snapshot.json 保存完整原始光学表；CSV 和 manifest 记录实际导出的采样点。",
    ]
    if export_stl:
        text += ["", "辅助 STL 路线：将 Lumerical 工作目录设为此导出目录，运行 import_stl.lsf。",
                 "main.lsf 与 import_stl.lsf 是两种替代导入路线，不应同时导入参与同一次仿真。",
                 "每个 STL 只含一个材料对象；STL 本身不保存材料、单位或重叠优先级，需配合脚本/manifest。"]
    text += ["", "边界与验证说明：", *["- " + warning for warning in warnings], ""]
    return "\n".join(text)
