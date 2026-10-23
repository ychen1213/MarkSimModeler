"""Generate an auditable, synthetic AH53 geometric acceptance project."""
from __future__ import annotations

import json
from pathlib import Path

from .geometry import build_geometry, save_geometry, preview_geometry
from .materials import import_material_folder, save_library
from .schemas import simulation_config


def _json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def create_ah53_project(folder, *, previews=True):
    """Compatibility entry: preserve the original AH53 acceptance configuration."""
    return _create_demo_project(folder, previews=previews)


def _create_demo_project(folder, *, previews=True, mark_config=None):
    """Create a NEW folder; never overwrite existing user inputs or results."""
    import gdstk
    folder = Path(folder).resolve()
    if folder.exists() and any(folder.iterdir()):
        raise FileExistsError(f"Refusing to overwrite existing project: {folder}")
    for child in ("materials", "inputs", "geometry", "runs"):
        (folder / child).mkdir(parents=True, exist_ok=True)
    family, period, height = "AH53", 16., .3
    cell_name, gds_name = "AH53_DEMO", "AH53_DEMO.gds"
    if mark_config is None:
        library = gdstk.Library(name="SYNTHETIC_AH53_DEMO", unit=1e-6, precision=1e-9)
        stripe = library.new_cell("STRIPE")
        stripe.add(gdstk.rectangle((-.8, -1), (.8, 1), layer=1, datatype=0))
        mark = library.new_cell("AH53_DEMO")
        mark.add(gdstk.Reference(stripe, origin=(-3.2, 0), columns=3, rows=1, spacing=(3.2, 0)))
        library.write_gds(folder / "inputs" / gds_name)
    else:
        from .marks import normalize_mark_config, write_mark_gds
        mark_config = normalize_mark_config(mark_config)
        family, period, height = mark_config["mark_type"], mark_config["mother_period_um"], mark_config["height_um"]
        gds_name = family+"_DEMO.gds"
        binding = write_mark_gds(folder/"inputs"/gds_name, mark_config)
        cell_name = binding["cell"]
    for name, n in (("AIR_DEMO", 1), ("SUBSTRATE_DEMO", 1.5), ("RIDGE_DEMO", 2.0)):
        (folder / "materials" / f"{name}.txt").write_text(
            f"# Synthetic constant-index acceptance material. Not an {family} material measurement.\n# wavelength unit: nm\nlambda,n,k\n400,{n},0\n643,{n},0\n1000,{n},0\n", encoding="utf-8")
    materials = import_material_folder(folder / "materials", wavelength_unit="nm")
    save_library(materials, folder / "materials" / "library.json")
    recipe = {"schema_version": "1.0", "model_version": "geometric-process-1.0",
              "period_x_um": period, "cell_bounds_um": [[-period/2, period/2], [-1, 1], [-2, max(1., height)]],
              "invariant_y": True, "background_material": "AIR_DEMO", "substrate_material": "SUBSTRATE_DEMO",
              "substrate_extend_bottom": True,
              "steps": [
                  {"id": "substrate", "name": "DEMO substrate", "operation": "create_substrate", "material": "SUBSTRATE_DEMO",
                   "z_min_um": -2.0, "z_max_um": 0.0, "enabled": True, "sidewall_angle_deg": 90},
                  {"id": "ridge", "name": "DEMO "+family+" ridge", "operation": "add", "material": "RIDGE_DEMO",
                   "mask": {"gds_path": "inputs/"+gds_name, "cell": cell_name, "layer": 1, "datatype": 0},
                   "mask_polarity": "positive", "z_min_um": 0, "thickness_um": height,
                   "cd_bias_um": 0, "delta_x_um": 0, "delta_y_um": 0, "rotation_deg": 0,
                   "enabled": True, "sidewall_angle_deg": 90}],
              "notes": "Three complete 1.6 um stripes centered -3.2,0,3.2. 50% internal subpitch duty; 60% of 8 um active region; 30% of 16 um mother period."}
    simulation = simulation_config({"schema_version": "1.0", "backend": "periodic_2d", "lambda_vacuum_nm": 643.0,
                  "period_x_um": period, "resolution_per_um": 40, "pml_um": 1.0,
                  "z_bounds_um": [-2.5, 3.0], "source_z_um": 1.6,
                  "reflection_monitor_z_um": 1.0, "transmission_monitor_z_um": -.7,
                  "phase_reference_z_um": .8, "fwidth_fraction": .04,
                  "after_source_time": 120.0, "tail_time": 30.0,
                  "max_wall_seconds": 3600, "resources": 1,
                  "incident_direction": [0., 0., -1.],
                  "polarizations": ["s", "p"], "weak_amplitude_threshold": 1e-5})
    project = {"schema_version": "1.0", "name": "AH53_SYNTHETIC_DEMO",
               "materials": "materials/library.json", "process": "process.yaml",
               "geometry": "geometry/geometry.json", "simulation": "simulation.yaml",
               "description": "Synthetic GDS and DEMO dielectric stack: software-chain verification only, not an actual AH53 optical prediction.",
               "ah53_geometry": {"mother_period_um": 16, "active_region_um": [-4, 4], "centers_um": [-3.2, 0, 3.2],
                                 "stripe_width_um": 1.6, "subpitch_um": 3.2, "height_um": .3,
                                 "duty_internal": .5, "fill_active_region": .6, "fill_mother_period": .3}}
    if mark_config is not None:
        from .marks import _geometry_mark
        recipe["mark"] = {**_geometry_mark(mark_config), "target_step_id": "ridge", "template_mask": binding}
        recipe["notes"] = "Parameterized public AH stripe template; nominal GDS dimensions precede process CD bias."
        project.update(name=family+"_PARAMETRIC_DEMO",
                       description="Public parametric AH geometry and DEMO materials; software verification, not a calibrated machine prediction.")
        project.pop("ah53_geometry", None)
    for name, value in (("project.yaml", project), ("process.yaml", recipe), ("simulation.yaml", simulation)):
        _json(folder / name, value)
    geometry = build_geometry(recipe, base_dir=folder, snapshots_dir=folder / "geometry" / "steps")
    save_geometry(geometry, folder / "geometry" / "geometry.json")
    if previews:
        preview_geometry(geometry, folder / "geometry")
    return {"project_dir": str(folder), "project": project, "geometry": geometry, "materials": materials,
            "simulation": simulation}
