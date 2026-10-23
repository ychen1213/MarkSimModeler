"""Verified structure snapshots, independent of the modeler installation.

An imported project freezes geometry AND material tables. Simulation settings
(including wavelength) remain editable. To change shapes or optical tables,
edit in the modeler and import a new bundle; raw material source paths are
provenance only and are never followed by this adapter.
"""
from __future__ import annotations
import copy
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
from .schemas import read_json, write_json, file_hash, canonical_hash, validate_geometry, versioned, simulation_config

FORMAT = "marksim-structure-bundle"
READ_ONLY = "导入结构与材料为只读快照；请在建模工具修改并重新导入新工程。仿真波长和设置可修改。"


def contained_file(root, relative):
    """Reject absolute, drive, traversal and symlink escapes on every platform."""
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Bundle references require nonempty relative POSIX paths")
    p = PurePosixPath(relative)
    if p.is_absolute() or PureWindowsPath(relative).drive or ".." in p.parts or relative != p.as_posix():
        raise ValueError("Bundle path must stay inside its directory")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"Missing or escaped bundle file: {relative}")
    return path


def read_bundle(source):
    path = Path(source).resolve()
    if path.is_dir(): path /= "structure.json"
    manifest = versioned(read_json(path), "structure bundle")
    if manifest.get("format") != FORMAT or manifest.get("units") != "um":
        raise ValueError("Expected marksim-structure-bundle with explicit um units")
    if manifest.get("coordinate_convention") != "x=scan,y=stripe,z=up":
        raise ValueError("Unsupported structure coordinate convention")
    hashes = manifest.get("files_sha256")
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("Bundle requires a nonempty files_sha256 manifest")
    files = {}
    for name, expected in hashes.items():
        target = contained_file(path.parent, name)
        if not isinstance(expected, str) or file_hash(target) != expected:
            raise ValueError(f"Structure bundle SHA256 mismatch: {name}")
        files[name] = target
    names = [manifest.get("geometry_file"), manifest.get("material_library_file")]
    if any(name not in files for name in names):
        raise ValueError("Geometry/material reference is not in verified manifest")
    geometry = validate_geometry(read_json(files[names[0]]))
    library = versioned(read_json(files[names[1]]), "MaterialLibrary")
    if not isinstance(library.get("materials"), dict) or not library["materials"]:
        raise ValueError("Bundle material library is empty")
    voxels = None
    if geometry["representation"] == "voxel_labels":
        from .voxel import load_voxels, create_voxel_model, geometry_manifest
        name = geometry.get("voxel_file")
        # A geometry reference is relative to geometry.json, but must remain a
        # verified bundle member, not merely exist somewhere beside the project.
        if name not in files or files[name] != contained_file(files[names[0]].parent, name):
            raise ValueError("Voxel reference must be a verified bundle-relative member")
        voxels = load_voxels(files[name])
        canonical = create_voxel_model(voxels["material_id"], voxels["origin_um"], voxels["spacing_um"],
            voxels["material_map"], background_id=voxels["background_id"], provenance=voxels.get("provenance"))
        expected = geometry_manifest(canonical, name)
        for key, value in expected.items():
            if key != "voxel_file" and canonical_hash(geometry.get(key)) != canonical_hash(value):
                raise ValueError(f"Voxel manifest disagrees with canonical HDF5 labels/coordinates: {key}")
        voxels = canonical
        used = set(voxels["material_map"].values())
    else:
        import gdstk
        for solid in geometry["solids"]:
            if gdstk.Polygon(solid["polygon_xy_um"]).area() <= 1e-18:
                raise ValueError("Degenerate zero-area solid polygon in structure bundle")
        used = {geometry["background_material"]} | {s["material"] for s in geometry["solids"]}
    if used - set(library["materials"]):
        raise ValueError(f"Structure has undefined materials: {sorted(used - set(library['materials']))}")
    return {"manifest_path":path,"manifest":manifest,"manifest_sha256":file_hash(path),
            "geometry":geometry,"materials":library,"voxels":voxels,"files":files}


def is_imported(data):
    return data.get("project", data).get("geometry_input", {}).get("kind") == "structure_bundle"


def imported_model(data):
    binding = data["project"].get("geometry_input", {})
    if binding.get("kind") != "structure_bundle": raise ValueError("Project is not a structure snapshot")
    source = contained_file(Path(data["folder"]).resolve(), binding.get("path"))
    if file_hash(source) != binding.get("manifest_sha256"):
        raise ValueError("Imported structure manifest changed; re-import as a new project")
    model = read_bundle(source)
    if canonical_hash(data["process"]) != binding.get("process_sha256"):
        raise ValueError(READ_ONLY)
    if canonical_hash(data["materials"]) != canonical_hash(model["materials"]):
        raise ValueError(READ_ONLY)
    return model


def input_binding(data):
    model = imported_model(data)
    return canonical_hash({"geometry_input":data["project"]["geometry_input"],
        "files_sha256":model["manifest"]["files_sha256"], "simulation":data["simulation"]})


def assert_solver_scope(geometry):
    """Read/preview accepts 3D; existing solvers remain periodic 2D only."""
    if geometry["representation"] != "parametric_solids" or not geometry.get("invariant_y"):
        raise ValueError("Imported geometry is previewable, but this solver requires y-invariant parametric periodic_2d solids; no automatic flattening")
    if geometry.get("periodic_x") is False or geometry.get("finite_array") or geometry.get("boundary_condition") == "finite":
        raise ValueError("Finite-array geometry cannot be silently solved as a periodic unit cell")


def preview_snapshot(data, output_dir):
    model = imported_model(data)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    geometry = copy.deepcopy(model["geometry"])
    if model["voxels"] is not None:
        from .voxel import save_voxels, preview_voxels
        geometry["voxel_file"] = "voxels.h5"
        save_voxels(model["voxels"], out/"voxels.h5")
        preview_voxels(model["voxels"], out)
    else:
        from .geometry import preview_geometry
        preview_geometry(geometry, out)
    write_json(out/"geometry.json", geometry)
    write_json(out/"import_binding.json", {"schema_version":"1.0", "manifest_sha256":model["manifest_sha256"]})
    return {"artifact_dir":str(out), "geometry":geometry}


def import_project(source, output, *, legacy_optics=False, previews=True):
    """Copy only verified bundle members into a NEW self-contained project."""
    model = read_bundle(source)
    folder = Path(output).resolve()
    if folder.exists() and any(folder.iterdir()):
        raise FileExistsError("Import requires a new or empty project directory; existing data will not be overwritten")
    if folder == model["manifest_path"].parent or folder.is_relative_to(model["manifest_path"].parent):
        raise ValueError("Imported project must be outside its source bundle")
    bundle = folder/"inputs"/"structure"
    bundle.mkdir(parents=True, exist_ok=False)
    for name, source_file in model["files"].items():
        target = bundle/name; target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source_file,target)
    shutil.copy2(model["manifest_path"], bundle/"structure.json")
    # Verify the copied bytes before referring to them in a runnable project.
    copied = read_bundle(bundle)
    geometry = copied["geometry"]
    process = {"schema_version":"1.0", "model_version":"imported-structure-1.0",
        "period_x_um":geometry.get("period_x_um", geometry["cell_bounds_um"][0][1]-geometry["cell_bounds_um"][0][0]),
        "cell_bounds_um":geometry["cell_bounds_um"], "background_material":geometry.get("background_material", ""),
        "invariant_y":geometry.get("invariant_y", False), "steps":[], "notes":READ_ONLY}
    simulation = simulation_config({"schema_version":"1.0", "period_x_um":process["period_x_um"]})
    project = {"schema_version":"1.0", "name":folder.name,"process":"process.yaml",
        "materials":"materials/library.json", "simulation":"simulation.yaml", "geometry":"geometry/imported/geometry.json",
        "description":READ_ONLY,"geometry_input":{"kind":"structure_bundle", "path":"inputs/structure/structure.json",
            "manifest_sha256":copied["manifest_sha256"],"process_sha256":canonical_hash(process)}}
    if legacy_optics:
        # Existing GUI expects a complete optics document even for FDTD-only
        # imports. This is a compatibility default, never executed on import.
        project["optics"] = "optics.yaml"
        write_json(folder/"optics.yaml", {"schema_version":"1.0", "model":"ideal_smash_equivalent",
            "incident_jones":[[1,0],[0,0]],"collection_na":.6,"include_zero":False,"max_abs_order":8,
            "scan_start_um":-4,"scan_stop_um":4,"scan_num_points":801,"rigid_shift_um":0.,
            "phase_delay_rad":0.,"reference_mode":"none","note":"Compatibility configuration only; structure import does not run optical readout."})
    for name, value in (("project.yaml", project),("process.yaml", process),("simulation.yaml", simulation),("materials/library.json", copied["materials"])):
        write_json(folder/name, value)
    data = {"folder":folder,"project":project,"process":process,"materials":copied["materials"],"simulation":simulation}
    result = {"project_dir":str(folder), "input_mode":"structure_bundle", "material_policy":"immutable snapshot; original source paths are provenance only",
        "solver_scope":"Preview/import is not solver acceptance; run validate to check the existing periodic_2d restrictions"}
    if previews: result.update(preview_snapshot(data,folder/"geometry/imported"))
    return result
