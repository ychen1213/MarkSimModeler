"""Portable xyz material volumes and self-contained Meep input generation."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np


def _validate_grid(grid, library, background_material):
    spacing = np.asarray(grid["spacing_nm"], dtype=float)
    if spacing.shape != (3,) or not np.all(np.isfinite(spacing)) or np.any(spacing <= 0):
        raise ValueError("spacing_nm needs three positive finite values")
    axes = [np.asarray(grid[f"{axis}_nm"], dtype=float) for axis in "xyz"]
    for axis, values, step in zip("xyz", axes, spacing):
        if values.ndim != 1 or not len(values) or not np.all(np.isfinite(values)):
            raise ValueError(f"{axis}_nm must contain finite voxel centers")
        if len(values) > 1 and not np.allclose(np.diff(values), step, rtol=1e-7, atol=1e-8):
            raise ValueError(f"{axis}_nm must increase uniformly with spacing_nm")
    ids = np.asarray(grid["material_id"])
    if ids.shape != tuple(map(len, axes)) or not np.issubdtype(ids.dtype, np.integer):
        raise ValueError("material_id must be an integer xyz array matching x_nm,y_nm,z_nm")
    if ids.min() < 0 or ids.max() > 65535:
        raise ValueError("material_id is restricted to uint16 IDs")
    raw = grid.get("materials", dict(enumerate(library.names)))
    mapping = {int(key): value for key, value in raw.items()} if isinstance(raw, dict) else dict(enumerate(raw))
    for value in np.unique(ids):
        if int(value) not in mapping:
            raise ValueError(f"Material ID {value} is absent from grid['materials']")
    for name in mapping.values():
        library.validate_material(name)
    background_ids = [key for key, name in mapping.items() if name == background_material]
    if not background_ids:
        raise ValueError("Background material must be included in grid['materials']")
    if any(key < 0 or key > 65535 for key in mapping):
        raise ValueError("Material mapping IDs must be between 0 and 65535")
    return axes, spacing, ids.astype(np.uint16, copy=False), mapping, min(background_ids)


def export_all(output_dir, grid, library, wavelength_nm=633.0, background_material="air"):
    """Export voxel centers (nm), IDs, complex-relative-epsilon lookup and Meep.

    Meep length unit is 1 um. Frozen optical values / conductivity reproduce
    n,k only at wavelength_nm. Explicit meep_dispersion models remain dispersive.
    The resulting Meep files need only NumPy plus Meep, not this project.
    """
    axes, spacing, ids, mapping, background_id = _validate_grid(grid, library, background_material)
    wavelength_nm = float(wavelength_nm)
    optical, meep_models = {}, {}
    for ident, name in mapping.items():
        values = library.evaluate(name, wavelength_nm)
        optical[str(ident)] = {"name": name, **{key: float(values[key][0]) for key in ("n", "k", "epsilon_real", "epsilon_imag")},
                              "material_definition": library.metadata(name)}
        meep_models[str(ident)] = library.meep_spec(name, wavelength_nm)
    lower = np.array([values[0] - step / 2 for values, step in zip(axes, spacing)])
    upper = np.array([values[-1] + step / 2 for values, step in zip(axes, spacing)])
    n_max = max(value["n"] for value in optical.values())
    recommended = int(np.ceil(max(1000 / np.min(spacing), 20 * n_max * 1000 / wavelength_nm)))
    bottom_id = int(ids[0, 0, 0])
    substrate = None
    if bottom_id != background_id and np.all(ids[:, :, 0] == bottom_id):
        last_layer = 0
        while last_layer + 1 < ids.shape[2] and np.all(ids[:, :, last_layer + 1] == bottom_id):
            last_layer += 1
        substrate = {"material_id": bottom_id, "top_nm": float(axes[2][last_layer] + spacing[2] / 2),
                     "assumption": "Planar homogeneous bottom layers continue laterally and below grid into PML"}
    metadata = {
        "schema_version": 1, "axis_order": "xyz", "coordinate_unit": "nm",
        "coordinate_sampling": "voxel_centers", "epsilon_kind": "dimensionless_relative_permittivity",
        "convention": "n+i*k, passive k>=0, exp(-i*omega*t), mu_r=1",
        "wavelength_nm": wavelength_nm, "shape_xyz": list(ids.shape), "spacing_nm": spacing.tolist(),
        "bounds_nm": {"lower": lower.tolist(), "upper": upper.tolist()},
        "meep_length_unit_nm": 1000, "recommended_resolution_per_um": recommended,
        "resolution_note": "Starting estimate only; establish convergence versus voxel spacing and FDTD resolution",
        "background_material_id": background_id, "materials": optical,
        "regions": grid.get("regions", []),
        "region_id_definition": "0 is the unassigned background; positive IDs refer to exported region definitions when supplied",
        "meep_models": meep_models, "inferred_substrate": substrate,
        "model_scope": "Center-wavelength epsilon/conductivity, narrowband approximation unless explicit Drude/Lorentz model; no automatic broadband fit"
    }
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    count = max(mapping) + 1
    arrays = {key + "_by_id": np.full(count, np.nan) for key in ("n", "k", "epsilon_real", "epsilon_imag")}
    names = [""] * count
    for ident, name in mapping.items():
        names[ident] = name
        for key in ("n", "k", "epsilon_real", "epsilon_imag"):
            arrays[key + "_by_id"][ident] = optical[str(ident)][key]
    payload = {
        "x_nm": axes[0], "y_nm": axes[1], "z_nm": axes[2], "spacing_nm": spacing,
        "material_id": ids, "material_names": np.asarray(names), "wavelength_nm": wavelength_nm,
        "axis_order": np.asarray("xyz"), "background_material_id": background_id,
        "epsilon_real": arrays["epsilon_real_by_id"][ids].astype(np.float32),
        "epsilon_imag": arrays["epsilon_imag_by_id"][ids].astype(np.float32), **arrays
    }
    if "region_id" in grid:
        if np.asarray(grid["region_id"]).shape != ids.shape:
            raise ValueError("region_id must match material_id xyz shape")
        payload["region_id"] = np.asarray(grid["region_id"])
    npz = output / "structure.npz"
    np.savez_compressed(npz, **payload)
    lookup = output / "material_lookup.json"
    lookup.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    library_file = output / "materials_snapshot.json"
    library_file.write_text(json.dumps(library.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
    measurements = output / "material_optics.csv"
    with measurements.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["material_id", "material", "wavelength_nm", "n", "k", "epsilon_real", "epsilon_imag", "meep_D_conductivity_um_inv"])
        for ident, name in mapping.items():
            item, model = optical[str(ident)], meep_models[str(ident)]
            writer.writerow([ident, name, wavelength_nm, item["n"], item["k"], item["epsilon_real"], item["epsilon_imag"], model.get("D_conductivity", "explicit_dispersion")])
    vtk = output / "structure.vtk"
    with vtk.open("w", encoding="ascii", newline="\n") as stream:
        stream.write("# vtk DataFile Version 3.0\nGrating materials, geometry coordinates in nm\nASCII\nDATASET STRUCTURED_POINTS\n")
        stream.write("DIMENSIONS " + " ".join(str(size + 1) for size in ids.shape) + "\n")
        stream.write("ORIGIN " + " ".join(f"{value:.12g}" for value in lower) + "\n")
        stream.write("SPACING " + " ".join(f"{value:.12g}" for value in spacing) + "\n")
        stream.write(f"CELL_DATA {ids.size}\nSCALARS material_id unsigned_short 1\nLOOKUP_TABLE default\n")
        # VTK varies x fastest, hence Fortran flatten of our xyz array.
        np.savetxt(stream, ids.ravel(order="F"), fmt="%d")
    structure_file, runner_file = output / "meep_structure.py", output / "run_meep.py"
    structure_file.write_text(MEEP_STRUCTURE, encoding="utf-8")
    runner_file.write_text(MEEP_RUNNER, encoding="utf-8")
    result = {"npz": str(npz), "vtk": str(vtk), "material_lookup": str(lookup),
              "material_optics": str(measurements), "materials_snapshot": str(library_file),
              "meep_structure": str(structure_file), "run_meep": str(runner_file)}
    try:
        import h5py
    except ImportError:
        pass
    else:
        hdf5 = output / "structure.h5"
        with h5py.File(hdf5, "w") as handle:
            for key, value in payload.items():
                if np.asarray(value).dtype.kind == "U":
                    value = np.asarray(value, dtype=h5py.string_dtype("utf-8"))
                kwargs = {"compression": "gzip"} if np.asarray(value).ndim > 0 else {}
                handle.create_dataset(key, data=value, **kwargs)
            handle.attrs["axis_order"] = "xyz"
            handle.attrs["coordinate_unit"] = "nm"
            handle.attrs["epsilon_kind"] = "relative"
            handle.attrs["warning"] = "General data interchange. Not a standalone Meep epsilon_input_file for lossy/dispersive materials. Use meep_structure.py."
        result["hdf5"] = str(hdf5)
    return result


MEEP_STRUCTURE = r'''"""Self-contained voxel material loader. Length unit: 1 micrometer.

Coordinates in structure.npz are voxel centers in nm, ordered xyz; Meep
positions are translated about the volume's bounding-box center, in um.
The native callback returns background outside the volume. Set
extend_substrate=True to extend a detected planar substrate through PML.
"""
from pathlib import Path
import json
import math
import numpy as np

DIRECTORY = Path(__file__).resolve().parent


def read_structure(directory=None):
    folder = Path(directory) if directory else DIRECTORY
    status_file = folder / "run_status.json"
    if status_file.exists():
        status = json.loads(status_file.read_text(encoding="utf-8"))
        if status.get("status") != "complete" or not status.get("usable_for_simulation",False):
            raise ValueError("Export is incomplete, failed or cancelled; regenerate a complete structure before simulation")
    meta = json.loads((folder / "material_lookup.json").read_text(encoding="utf-8"))
    with np.load(folder / "structure.npz", allow_pickle=False) as archive:
        grid = {key: archive[key] for key in archive.files}
    if str(grid["axis_order"]) != "xyz" or list(grid["material_id"].shape) != meta["shape_xyz"]:
        raise ValueError("Axis order or volume shape mismatch")
    spacing = np.asarray(grid["spacing_nm"], dtype=float)
    if spacing.shape != (3,) or not np.all(np.isfinite(spacing)) or np.any(spacing <= 0):
        raise ValueError("Invalid voxel spacing")
    if not np.allclose(spacing, meta["spacing_nm"]):
        raise ValueError("NPZ and metadata voxel spacing disagree")
    for dimension, (axis, size) in enumerate(zip("xyz", grid["material_id"].shape)):
        centers = grid[axis + "_nm"]
        if centers.shape != (size,) or not np.all(np.isfinite(centers)):
            raise ValueError("Coordinate dimensions do not match voxel array")
        if size > 1 and not np.allclose(np.diff(centers), spacing[dimension], rtol=1e-7, atol=1e-8):
            raise ValueError("Coordinate axes must be uniform and increasing")
        bounds = [centers[0] - spacing[dimension] / 2, centers[-1] + spacing[dimension] / 2]
        if not np.allclose(bounds, [meta["bounds_nm"][side][dimension] for side in ("lower", "upper")]):
            raise ValueError("Coordinate bounds and metadata disagree")
    if not np.isclose(float(grid["wavelength_nm"]), meta["wavelength_nm"]):
        raise ValueError("Metadata and NPZ wavelengths disagree")
    for ident in np.unique(grid["material_id"]):
        if str(int(ident)) not in meta["materials"]:
            raise ValueError("Voxel refers to undefined material ID")
    for component in ("epsilon_real", "epsilon_imag"):
        if grid[component].shape != grid["material_id"].shape:
            raise ValueError("Permittivity volume shape mismatch")
        expected = grid[component + "_by_id"][grid["material_id"]]
        if not np.all(np.isfinite(expected)) or not np.allclose(grid[component], expected, rtol=1e-6, atol=1e-8):
            raise ValueError("Permittivity volume and material-ID lookup disagree")
    if "region_id" in grid and grid["region_id"].shape != grid["material_id"].shape:
        raise ValueError("Region-ID shape mismatch")
    return grid, meta


def validate(directory=None, broadband=False):
    grid, meta = read_structure(directory)
    if broadband:
        for item in meta["materials"].values():
            definition = item["material_definition"]
            if definition["model"] == "meep_dispersion":
                continue
            if definition["model"] == "constant" and item["k"] == 0:
                continue
            raise ValueError(item["name"] + ": broadband requires a fitted Drude/Lorentz model; tabulated n,k and conductivity are insufficient")
    for ident, item in meta["materials"].items():
        model = meta["meep_models"][ident]
        n, k, er, ei = (item[key] for key in ("n", "k", "epsilon_real", "epsilon_imag"))
        if not np.all(np.isfinite([n, k, er, ei])) or min(n, k) < 0:
            raise ValueError("Material optics must be finite and passive")
        if not np.allclose([er, ei], [n * n - k * k, 2 * n * k], rtol=1e-8, atol=1e-10):
            raise ValueError("n,k and relative permittivity disagree")
        material_id = int(ident)
        for key in ("n", "k", "epsilon_real", "epsilon_imag"):
            if not np.isclose(grid[key + "_by_id"][material_id], item[key], rtol=1e-8, atol=1e-10):
                raise ValueError("JSON material optics and NPZ lookup disagree")
        if model["mode"] == "narrowband" and model["epsilon_real"] <= 0:
            raise ValueError(item["name"] + ": nonpositive epsilon needs Drude/Lorentz dispersion")
        if model["mode"] == "narrowband":
            f = 1000 / meta["wavelength_nm"]
            if not np.allclose([model["epsilon_real"], model["epsilon_real"] * model["D_conductivity"] / (2 * math.pi * f)], [er, ei], rtol=1e-8, atol=1e-10):
                raise ValueError("Meep epsilon/conductivity do not match exported optical data")
    return {"shape_xyz": meta["shape_xyz"], "wavelength_nm": meta["wavelength_nm"],
            "material_names": [item["name"] for item in meta["materials"].values()],
            "recommended_resolution_per_um": meta["recommended_resolution_per_um"],
            "substrate_extension_available": meta["inferred_substrate"] is not None,
            "note": meta["model_scope"]}


def load_structure(directory=None, wavelength_nm=None, broadband=False, extend_substrate=False):
    validate(directory, broadband=broadband)
    import meep as mp
    grid, meta = read_structure(directory)
    if wavelength_nm is not None and not np.isclose(wavelength_nm, meta["wavelength_nm"], rtol=0, atol=1e-6):
        raise ValueError("Optics are evaluated at the exported wavelength; regenerate the export to change wavelength")
    media = {}
    for ident, model in meta["meep_models"].items():
        if model["mode"] == "dispersive":
            poles = []
            for pole in model["electric_susceptibilities"]:
                constructor = mp.LorentzianSusceptibility if pole["kind"] == "lorentz" else mp.DrudeSusceptibility
                poles.append(constructor(frequency=pole["frequency_um_inv"], gamma=pole["gamma_um_inv"], sigma=pole["sigma"]))
            limits = model["valid_range_nm"]
            medium = mp.Medium(epsilon=model["epsilon_inf"], E_susceptibilities=poles,
                               valid_freq_range=mp.FreqRange(min=1000 / limits[1], max=1000 / limits[0]))
        else:
            medium = mp.Medium(epsilon=model["epsilon_real"], D_conductivity=model["D_conductivity"])
        media[int(ident)] = medium
    lower = np.asarray(meta["bounds_nm"]["lower"])
    upper = np.asarray(meta["bounds_nm"]["upper"])
    center = (lower + upper) / 2
    spacing = np.asarray(meta["spacing_nm"])
    ids = grid["material_id"]
    background = media[meta["background_material_id"]]
    substrate = meta["inferred_substrate"]
    if extend_substrate and substrate is None:
        raise ValueError("No homogeneous bottom substrate detected; explicitly design material continuation into the lower PML")

    def material_function(point):
        physical_nm = np.array([point.x, point.y, point.z]) * 1000 + center
        if extend_substrate and physical_nm[2] < substrate["top_nm"]:
            return media[substrate["material_id"]]
        index = np.floor((physical_nm - lower) / spacing).astype(int)
        if np.any(index < 0) or np.any(index >= ids.shape):
            return background
        return media[int(ids[tuple(index)])]

    return {"material_function": material_function, "extra_materials": list(media.values()),
            "materials": media, "background_material": background, "grid": grid, "metadata": meta,
            "bounds_um": {"lower": (lower - center) / 1000, "upper": (upper - center) / 1000},
            "cell_size_um": (upper - lower) / 1000,
            "recommended_resolution": meta["recommended_resolution_per_um"],
            "frequency": 1000 / meta["wavelength_nm"]}
'''


MEEP_RUNNER = r'''"""Meep finite-mark starting simulation; not a calibrated reflectance model.

python run_meep.py --check-only                 # NumPy only, no Meep required
python run_meep.py --run --resolution 100       # In a working Meep environment

All boundaries use PML because measured roughness is not generally periodic.
The inferred bottom substrate is extended through PML; source is above the
sample, polarized Ex. Raw central-frequency flux is not normalized R or T.
Converge resolution, PML, padding, duration, and material model for research.
"""
from pathlib import Path
import argparse
import json
import numpy as np
from meep_structure import validate, load_structure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check-only", action="store_true")
    action.add_argument("--run", action="store_true")
    parser.add_argument("--resolution", type=float, default=None, help="FDTD cells per um; default estimated from voxel size and index")
    parser.add_argument("--max-cells", type=int, default=20000000, help="Refuse runs exceeding this estimated cell count; increase explicitly only after checking memory")
    parser.add_argument("--pml-um", type=float, default=0.5)
    parser.add_argument("--padding-um", type=float, default=0.75)
    parser.add_argument("--duration", type=float, default=100.0, help="Meep time units after pulse; converge before interpreting flux")
    parser.add_argument("--fractional-bandwidth", type=float, default=0.02, help="Pulse bandwidth/reference frequency; fixed material data approximate a narrow band")
    args = parser.parse_args()
    for key in ("pml_um", "padding_um", "duration", "fractional_bandwidth"):
        if not np.isfinite(getattr(args, key)) or getattr(args, key) <= 0:
            parser.error(key + " must be positive and finite")
    if args.resolution is not None and (not np.isfinite(args.resolution) or args.resolution <= 0):
        parser.error("resolution must be positive and finite")
    if args.max_cells <= 0:
        parser.error("max-cells must be a positive integer")
    if args.fractional_bandwidth > 0.05:
        parser.error("This runner uses center-wavelength optical constants. Wider pulses require explicit broadband material models and a custom driver.")
    info = validate()
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if args.check_only:
        return
    try:
        import meep as mp
    except ImportError as exc:
        raise SystemExit("Meep is not installed. Use a Linux/WSL/conda environment with the official pymeep package; PyPI 'meep' is not the MIT FDTD package.") from exc
    structure = load_structure(extend_substrate=info["substrate_extension_available"])
    extent = structure["cell_size_um"]
    pml, pad = args.pml_um, args.padding_um
    cell = extent + 2 * (pml + pad)
    top = structure["bounds_um"]["upper"][2]
    source_z = top + 0.7 * pad
    reflection_z = top + 0.4 * pad
    resolution = args.resolution or structure["recommended_resolution"]
    approximate_cells = int(np.prod(np.ceil(cell * resolution)))
    if approximate_cells > args.max_cells:
        parser.error(f"Estimated {approximate_cells:,} FDTD cells exceed --max-cells {args.max_cells:,}. Lower --resolution for an initial setup check, or explicitly increase --max-cells after estimating available RAM. Refine resolution and test convergence for scientific results.")
    print(f"Starting finite 3D example: approximately {approximate_cells:,} FDTD cells; actual RAM depends on fields and dispersive terms.")
    # A finite current sheet radiates toward +/-z. Monitor below the sheet is
    # a raw total-field flux plane, not separated incident/reflected flux.
    source_size = mp.Vector3(extent[0], extent[1], 0)
    frequency = structure["frequency"]
    sources = [mp.Source(mp.GaussianSource(frequency, fwidth=frequency * args.fractional_bandwidth),
                         component=mp.Ex, center=mp.Vector3(z=source_z), size=source_size)]
    sim = mp.Simulation(cell_size=mp.Vector3(*cell), resolution=resolution, dimensions=3,
                        boundary_layers=[mp.PML(pml)], sources=sources,
                        material_function=structure["material_function"],
                        extra_materials=structure["extra_materials"], eps_averaging=False,
                        filename_prefix="grating")
    sim.use_output_directory(str(Path(__file__).resolve().parent / "meep_results"))
    flux = sim.add_flux(frequency, 0, 1,
                       mp.FluxRegion(center=mp.Vector3(z=reflection_z), size=source_size))
    sim.run(mp.at_beginning(mp.output_epsilon), until_after_sources=args.duration)
    raw_flux = mp.get_fluxes(flux)
    result = {"wavelength_nm": info["wavelength_nm"], "raw_total_flux": raw_flux,
              "resolution_per_um": resolution, "pml_um": pml, "padding_um": pad,
              "note": "Unnormalized raw total-field flux, not R or T; add reference simulation and incident-field subtraction for reflectance.",
              "material_scope": info["note"]}
    (Path(__file__).resolve().parent / "meep_results" / "raw_flux.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
'''
