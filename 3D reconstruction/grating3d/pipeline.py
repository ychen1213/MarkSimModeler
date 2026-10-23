"""Reconstruction orchestration, material regions, and portable voxel grids."""
from __future__ import annotations

import copy
import csv
import hashlib
import json
from pathlib import Path

import numpy as np
from .path_compat import resolve_input_path

ROOT = Path(__file__).resolve().parents[1]


class PipelineCancelled(RuntimeError):
    """Cooperative cancellation at stages; incomplete outputs are marked."""


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise PipelineCancelled("已取消任务；未完成的输出不能作为仿真结构使用。")


def measurement_signature(config):
    keys = ("generate_demo","sem_nm_per_pixel","sem_nm_per_pixel_y","tem_nm_per_pixel","tem_nm_per_pixel_y",
            "calibration","sem_processing","tem_processing","geometry","synthetic")
    selected = {key:config.get(key) for key in keys}
    if not config.get("generate_demo"):
        for key in ("sem_image","tem_image"):
            path = Path(config[key]).resolve()
            stat = path.stat()
            selected[key] = [str(path),stat.st_size,stat.st_mtime_ns]
    return hashlib.sha256(json.dumps(json_ready(selected),sort_keys=True).encode("utf-8")).hexdigest()


def _calibration_for(config, name):
    from .preprocessing import calibration_scales
    supplied = config.get("calibration",{}).get(name,{})
    if supplied and not config.get("generate_demo") and not supplied.get("confirmed",False):
        raise ValueError(f"{name.upper()} 真实图尚未校准：请用标尺或设备值完成校准。")
    if "nm_per_pixel_x" in supplied and "nm_per_pixel_y" in supplied:
        return calibration_scales([supplied["nm_per_pixel_x"],supplied["nm_per_pixel_y"]])
    sx, sy = calibration_scales(config[name+"_nm_per_pixel"])
    sy = config.get(name+"_nm_per_pixel_y",config.get(name+"_processing",{}).get("nm_per_pixel_y",sy))
    return calibration_scales([sx,sy])


def estimate_grid(geometry, config):
    import math
    settings = config.get("grid",{})
    step = np.asarray(settings.get("spacing_nm",[15.,15.,10.]),dtype=float)
    if step.ndim==0:
        step=np.repeat(step,3)
    if step.shape!=(3,) or not np.isfinite(step).all() or (step<=0).any():
        raise ValueError("体素间距必须是三个正数。")
    depth, padding = float(settings.get("substrate_depth_nm",200)),float(settings.get("air_padding_nm",250))
    if not np.isfinite([depth,padding]).all() or min(depth,padding)<=0:
        raise ValueError("基底厚度和空气余量必须是正数。")
    height=float(np.max(geometry.get("ridge_heights_nm",[geometry["height_nm"]])))
    bounds=[geometry["x_bounds_nm"],geometry["y_bounds_nm"],[-depth,height+padding]]
    shape=[max(2,int(np.ceil((b[1]-b[0])/s))) for b,s in zip(bounds,step)]
    count=math.prod(shape)
    return {"shape_xyz":shape,"voxel_count":count,"count":count,"bounds_nm":bounds,
            "requested_spacing_nm":step.tolist(),"estimated_memory_mb":count*32/(1024**2),
            "memory_note":"仅重建数组粗估；不含求解器、绘图和复制开销"}


def prepare_measurements(config, log=lambda msg:None, cancel_event=None):
    """Image measurement and geometry review do not require wavelength/materials."""
    from .synthetic import generate_demo
    from .imaging import extract_sem,extract_tem
    from .geometry import build_geometry
    config=copy.deepcopy(config)
    signature=measurement_signature(config)
    _check_cancel(cancel_event)
    sem_scale,tem_scale=_calibration_for(config,"sem"),_calibration_for(config,"tem")
    generated=None
    if config.get("generate_demo"):
        log("Generating independent known-truth synthetic images...")
        settings=copy.deepcopy(config.get("synthetic",{}))
        settings.update(sem_nm_per_pixel=sem_scale[0],sem_nm_per_pixel_y=sem_scale[1],
                        tem_nm_per_pixel=tem_scale[0],tem_nm_per_pixel_y=tem_scale[1])
        settings.setdefault("sem_shape",(int(np.ceil(1320/sem_scale[1])),int(np.ceil(2760/sem_scale[0]))))
        settings.setdefault("tem_shape",(int(np.ceil(585/tem_scale[1])),int(np.ceil(2760/tem_scale[0]))))
        if np.prod(settings["sem_shape"])+np.prod(settings["tem_shape"])>25000000:
            raise ValueError("测试图超过2500万像素，请增加nm/pixel或缩小图像。")
        output=Path(config.get("output_dir",ROOT/"outputs"/"run")).resolve()
        generated=generate_demo(output/"synthetic",settings)
        config.update(sem_image=generated["sem_path"],tem_image=generated["tem_path"],truth_file=generated["truth_path"])
    _check_cancel(cancel_event)
    log("Extracting SEM top edges...")
    sem=extract_sem(config["sem_image"],sem_scale,config.get("sem_processing"))
    _check_cancel(cancel_event)
    log("Extracting TEM cross-section and sidewalls...")
    tem=extract_tem(config["tem_image"],tem_scale,config.get("tem_processing"))
    _check_cancel(cancel_event)
    log("Building geometry for edge review...")
    geometry=build_geometry(sem,tem,config.get("geometry",{}))
    warnings=list(dict.fromkeys(sem.get("warnings",[])+tem.get("warnings",[])+geometry.get("warnings",[])))
    if not config.get("generate_demo") and not config.get("calibration"):
        warnings.append("采用配置中的标尺数值，但未记录校准来源；请核对真实图的设备标尺。")
    warnings.extend(["SEM与TEM的条数相同并不证明来自同一位置；请核对切片方向和对应条号。",
                     "TEM截面之外的侧壁沿光栅方向延拓；外推区域不代表额外测量。"])
    if "ridge_active" in sem and not np.all(sem["ridge_active"]):
        warnings.append("保留的SEM缺口按光栅全高空缺建模；俯视图不能确认缺口下面是否存在连续材料。")
    quality={"status":"review","warnings":warnings,"diagnostics":{"sem":sem.get("diagnostics",{}),"tem":tem.get("diagnostics",{}),
        "edge_valid_fraction":sem["metrics"].get("edge_valid_fraction"),"sidewall_fit_rms_mean_nm":tem["metrics"].get("sidewall_fit_rms_mean_nm"),
        "height_std_nm":tem["metrics"].get("height_std_nm")}}
    return {"config":config,"sem":sem,"tem":tem,"geometry":geometry,"quality":quality,
            "input_signature":signature,"synthetic":generated}


def json_ready(value):
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(v) for v in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    return value


def save_json(path, value):
    Path(path).write_text(json.dumps(json_ready(value), ensure_ascii=False, indent=2), encoding="utf-8")


def load_config(path):
    path = resolve_input_path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if config.get("schema_version", 1) != 1:
        raise ValueError("Unsupported configuration schema_version")
    for key in ("sem_image", "tem_image", "material_library", "output_dir", "truth_file"):
        if config.get(key):
            config[key] = str(resolve_input_path(config[key], base_dir=path.parent).resolve())
    return config


def demo_config(output_dir):
    config = load_config(ROOT / "configs" / "example.json")
    config.update(output_dir=str(Path(output_dir).resolve()), generate_demo=True,
                  calibration_note="Synthetic generator uses known 3 nm/px; image processing does not read truth.")
    return config


def _axis(bounds, requested_step):
    lo, hi = map(float, bounds)
    if not np.isfinite([lo, hi, requested_step]).all() or hi <= lo or requested_step <= 0:
        raise ValueError("Grid bounds and spacing must be finite, with positive span and spacing")
    count = max(2, int(np.ceil((hi - lo) / requested_step)))
    step = (hi - lo) / count
    return lo + (np.arange(count) + 0.5) * step, step


def build_material_grid(geometry, config, library):
    from .geometry import ridge_mask
    settings = config.get("grid", {})
    requested = np.asarray(settings.get("spacing_nm", [15, 15, 10]), dtype=float)
    if requested.ndim == 0:
        requested = np.repeat(requested, 3)
    if requested.shape != (3,) or not np.isfinite(requested).all() or (requested <= 0).any():
        raise ValueError("grid.spacing_nm must be three positive finite values")
    depth = float(settings.get("substrate_depth_nm", 200))
    padding = float(settings.get("air_padding_nm", 250))
    if not np.isfinite([depth, padding]).all() or min(depth, padding) <= 0:
        raise ValueError("substrate_depth_nm and air_padding_nm must be positive finite values")
    height = float(np.max(geometry.get("ridge_heights_nm", [geometry["height_nm"]])))
    bounds = [geometry["x_bounds_nm"], geometry["y_bounds_nm"], [-depth, height + padding]]
    count = estimate_grid(geometry,config)["voxel_count"]
    limit = int(settings.get("max_voxels", 12000000))
    if count > limit:
        raise ValueError(f"Grid needs {count:,} voxels, exceeds max_voxels={limit:,}; increase spacing")
    axes, spacing = zip(*[_axis(b, d) for b, d in zip(bounds, requested)])
    x, y, z = axes
    background = config.get("background_material", "air")
    regions = config.get("regions", [])
    if not isinstance(regions, list):
        raise ValueError("regions must be a JSON list")
    for region in regions:
        if not np.isfinite(float(region.get("priority", 0))):
            raise ValueError("Region priority must be a finite number")
    names = list(dict.fromkeys([background] + [r["material"] for r in regions]))
    for name in names:
        library.validate_material(name)
        library.evaluate(name, [float(config["wavelength_nm"])])
    if len(names) > 65535 or len(regions) > 65534:
        raise ValueError("Too many materials or regions for uint16 IDs")
    materials = dict(enumerate(names))
    name_to_id = {name: i for i, name in materials.items()}
    material_id = np.zeros((len(x), len(y), len(z)), dtype=np.uint16)
    region_id = np.zeros_like(material_id)
    ridge = ridge_mask(geometry, x, y, z)
    region_details = []
    for original_index, region in sorted(enumerate(regions), key=lambda item: float(item[1].get("priority", 0))):
        kind = region.get("kind")
        if kind == "substrate":
            mask = np.broadcast_to(z[None, None, :] < 0, material_id.shape)
        elif kind in ("ridge", "ridge_layer"):
            if "ridge_indices" in region:
                selected = region["ridge_indices"]
                n = len(geometry["left_top_nm"])
                if not selected or any(not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < n for i in selected):
                    raise ValueError(f"{region.get('name')}: ridge_indices must contain valid zero-based indices")
                subset = copy.copy(geometry)
                for key in ("left_top_nm", "right_top_nm", "bottom_left_offset_nm", "bottom_right_offset_nm", "ridge_heights_nm", "bowing_nm"):
                    if key in subset:
                        subset[key] = np.asarray(subset[key])[selected]
                subset["ridge_count"] = len(selected)
                for key in ("sidewall_profiles","active_intervals_nm"):
                    if geometry.get(key) is not None:
                        subset[key]=[geometry[key][i] for i in selected]
                mask = ridge_mask(subset, x, y, z)
            else:
                mask = ridge.copy()
            if kind == "ridge_layer":
                lo, hi = float(region["z_min_nm"]), float(region["z_max_nm"])
                if not np.isfinite([lo, hi]).all() or hi <= lo:
                    raise ValueError("ridge_layer requires finite z_min_nm < z_max_nm")
                mask = mask & ((z >= lo) & (z < hi))[None, None, :]
        elif kind in ("box", "layer"):
            mask = np.ones(material_id.shape, dtype=bool)
            for dim, (key, axis) in enumerate(zip(("x_range_nm", "y_range_nm", "z_range_nm"), axes)):
                if key not in region:
                    if kind == "box" or key == "z_range_nm":
                        raise ValueError(f"{kind} region requires {key}")
                    continue
                lo, hi = map(float, region[key])
                if not np.isfinite([lo, hi]).all() or hi <= lo:
                    raise ValueError(f"{key} must be two finite increasing values")
                shape = [1, 1, 1]
                shape[dim] = len(axis)
                mask &= ((axis >= lo) & (axis < hi)).reshape(shape)
        else:
            raise ValueError(f"Unsupported region kind {kind!r}")
        material_id[mask] = name_to_id[region["material"]]
        region_id[mask] = original_index + 1
        region_details.append({**region, "region_id": original_index + 1, "selected_voxels": int(mask.sum())})
    for region in region_details:
        region["final_voxels"] = int(np.count_nonzero(region_id == region["region_id"]))
    return {"x_nm": x, "y_nm": y, "z_nm": z, "spacing_nm": list(spacing),
            "requested_spacing_nm": requested.tolist(), "bounds_nm": bounds,
            "material_id": material_id, "region_id": region_id, "materials": materials,
            "regions": region_details, "background_material": background, "substrate_top_nm": 0.0}


def plot_material_slices(grid, output_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap, BoundaryNorm
    palette = ["#e8eff4", "#304f89", "#e8ac50", "#8b63a7", "#37b8a3", "#d56569"]
    n = len(grid["materials"])
    cmap = ListedColormap([palette[i % len(palette)] for i in range(n)])
    norm = BoundaryNorm(np.arange(n + 1) - 0.5, n)
    x, y, z = [grid[f"{a}_nm"] for a in "xyz"]
    b = grid["bounds_nm"]
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
    image = axes[0].imshow(grid["material_id"][:, len(y) // 2, :].T, origin="lower", aspect="auto", extent=[*b[0], *b[2]], cmap=cmap, norm=norm, interpolation="nearest")
    axes[0].set(xlabel="x (nm)", ylabel="z (nm)", title=f"Material cross section at y={y[len(y)//2]:.1f} nm")
    zi = int(np.argmin(abs(z - 0.8 * max(z[z > 0]))))
    # Pick a plane within ridge height rather than within the empty air padding.
    occupied = np.where(np.any(grid["material_id"] != 0, axis=(0, 1)) & (z >= 0))[0]
    if occupied.size:
        zi = int(occupied[len(occupied) // 2])
    axes[1].imshow(grid["material_id"][:, :, zi].T, origin="lower", aspect="auto", extent=[*b[0], *b[1]], cmap=cmap, norm=norm, interpolation="nearest")
    axes[1].set(xlabel="x (nm)", ylabel="y (nm)", title=f"Material plan view at z={z[zi]:.1f} nm")
    cb = fig.colorbar(image, ax=axes, ticks=range(n), shrink=0.75)
    cb.ax.set_yticklabels([grid["materials"][i] for i in range(n)])
    path = Path(output_dir) / "material_slices.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return str(path)


def interactive_preview(geometry, path):
    import plotly.graph_objects as go
    from .geometry import triangulate_ridges
    vertices, faces = triangulate_ridges(geometry, y_stride=4, z_samples=9)
    fig = go.Figure(go.Mesh3d(x=vertices[:,0], y=vertices[:,1], z=vertices[:,2],
                            i=faces[:,0], j=faces[:,1], k=faces[:,2], color="#55a7d2",
                            flatshading=False, name="Reconstructed grating", showscale=False))
    fig.update_layout(title="SEM/TEM reconstruction (nm) - inferred sidewalls", scene=dict(xaxis_title="x (nm)", yaxis_title="y (nm)", zaxis_title="z (nm)", aspectmode="data"), margin=dict(l=0,r=0,b=0,t=45))
    fig.write_html(str(path), include_plotlyjs=True)
    return str(path)


def export_csv(grid, library, wavelength_nm, path):
    lookups = {i: library.evaluate(name, [wavelength_nm]) for i, name in grid["materials"].items()}
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["x_nm", "y_nm", "z_nm", "material_id", "material", "region_id", "wavelength_nm", "n", "k", "epsilon_real", "epsilon_imag"])
        for ix, x in enumerate(grid["x_nm"]):
            for iy, y in enumerate(grid["y_nm"]):
                for iz, z in enumerate(grid["z_nm"]):
                    ident = int(grid["material_id"][ix, iy, iz])
                    opt = lookups[ident]
                    writer.writerow([x, y, z, ident, grid["materials"][ident], int(grid["region_id"][ix,iy,iz]), wavelength_nm, *[float(opt[k][0]) for k in ("n", "k", "epsilon_real", "epsilon_imag")]])


def run_pipeline(config, log=lambda msg:None, cancel_event=None, measurements=None):
    output=Path(config.get("output_dir",ROOT/"outputs"/"run")).resolve()
    output.mkdir(parents=True,exist_ok=True)
    status_path=output/"run_status.json"
    save_json(status_path,{"status":"running","usable_for_simulation":False})
    try:
        result=_run_pipeline_impl(config,log,cancel_event,measurements)
    except PipelineCancelled as exc:
        save_json(status_path,{"status":"cancelled","usable_for_simulation":False,"message":str(exc)})
        raise
    except Exception as exc:
        save_json(status_path,{"status":"failed","usable_for_simulation":False,"message":str(exc)})
        raise
    save_json(status_path,{"status":"complete","usable_for_simulation":True})
    return result


def _run_pipeline_impl(config, log, cancel_event, measurements):
    from .imaging import plot_edge_overlays
    from .geometry import write_stl, plot_preview
    from .materials import MaterialLibrary
    from .exporters import export_all
    config = copy.deepcopy(config)
    wavelength = float(config.get("wavelength_nm", 633))
    if not np.isfinite(wavelength) or wavelength <= 0:
        raise ValueError("wavelength_nm must be a positive finite number")
    config["wavelength_nm"] = wavelength
    output = Path(config.get("output_dir", ROOT / "outputs" / "run")).resolve()
    output.mkdir(parents=True, exist_ok=True)
    _check_cancel(cancel_event)
    if measurements is None:
        measurements=prepare_measurements(config,log,cancel_event)
    elif measurements.get("input_signature")!=measurement_signature(config):
        raise ValueError("图像、标尺或轮廓设置已改变，请重新提取并核对边缘后导出。")
    for key in ("sem_image","tem_image","truth_file"):
        if measurements["config"].get(key):
            config[key]=measurements["config"][key]
    generated=measurements.get("synthetic")
    sem,tem,geometry=[measurements[k] for k in ("sem","tem","geometry")]
    library = MaterialLibrary(config.get("material_library"))
    _check_cancel(cancel_event)
    log("Assigning spatial materials and exporting solver inputs...")
    grid = build_material_grid(geometry, config, library)
    _check_cancel(cancel_event)
    files = export_all(output, grid, library, wavelength, config.get("background_material", "air"))
    _check_cancel(cancel_event)
    spectra_path = output / "material_spectra.csv"
    with spectra_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["material_id", "material", "wavelength_nm", "n", "k", "epsilon_real", "epsilon_imag"])
        for ident, name in grid["materials"].items():
            spec = library.snapshot()["materials"][name]
            if spec["model"] == "tabulated":
                waves = np.unique(np.r_[np.asarray(spec["data"])[:,0], wavelength])
            elif spec.get("valid_range_nm") and spec["model"] != "constant":
                waves = np.unique(np.r_[np.linspace(*spec["valid_range_nm"], 200), wavelength])
            else:
                waves = np.array([wavelength])
            data = library.evaluate(name, waves)
            for row in zip(*[data[k] for k in ("wavelength_nm", "n", "k", "epsilon_real", "epsilon_imag")]):
                writer.writerow([ident, name, *row])
    files["material_spectra"] = str(spectra_path)
    log("Rendering measured edges and 3D previews...")
    files.update(plot_edge_overlays(sem, tem, output))
    _check_cancel(cancel_event)
    stl = output / "grating_geometry_um.stl"
    mesh_info = write_stl(geometry, stl)
    files["geometry_stl"] = str(stl)
    preview = output / "reconstruction_3d.png"
    plot_preview(geometry, preview)
    files["geometry_preview"] = str(preview)
    files["interactive_preview"] = interactive_preview(geometry, output / "reconstruction_3d.html")
    _check_cancel(cancel_event)
    files["material_slices"] = plot_material_slices(grid, output)
    np.savez_compressed(output / "measured_edges.npz", y_nm=sem["y_nm"], left_nm=sem["left_nm"], right_nm=sem["right_nm"],
                        raw_left_nm=sem.get("raw_left_nm",sem["left_nm"]),raw_right_nm=sem.get("raw_right_nm",sem["right_nm"]),
                        ridge_active=sem.get("ridge_active",np.ones(sem["left_nm"].shape,dtype=bool)),
                        edge_valid_mask=sem.get("edge_valid_mask",np.ones(sem["left_nm"].shape,dtype=bool)),
                        edge_interpolated_mask=sem.get("edge_interpolated_mask",np.zeros(sem["left_nm"].shape,dtype=bool)))
    files["measured_edges"] = str(output / "measured_edges.npz")
    if config.get("export_voxel_csv") or config.get("export_csv"):
        log("Writing full voxel CSV (can be large)...")
        export_csv(grid, library, wavelength, output / "voxels.csv")
        files["voxel_csv"] = str(output / "voxels.csv")
    warnings = [
        "One SEM plan view and one TEM section do not uniquely determine arbitrary 3D morphology; sidewall profiles away from the section are assumed.",
        "Material identity and layer positions are user inputs, not inferred from grayscale.",
        "Finite observed mark; periodic boundaries require a separately justified repeatable unit cell.",
        "Voxel discretization error is separate from image measurement error; refine the grid and perform solver convergence tests.",
        "Meep narrowband conductivity matches absorption at the stated center wavelength; tabulated n,k are not automatically a causal broadband model.",
        "STL contains only grating ridge geometry, in um, and does not include substrate or material regions; use material-ID volume for multimaterial simulation."]
    warnings.extend(measurements["quality"].get("warnings",[]))
    for r in grid["regions"]:
        if not r["final_voxels"]:
            warnings.append(f"Region {r.get('name',r['region_id'])!r} has no final voxels: outside domain, sub-voxel thickness, or overwritten by higher priority.")
    report = {"schema_version": 1, "wavelength_nm": wavelength, "time_convention": "exp(-i omega t)", "epsilon_type": "dimensionless relative complex permittivity; mu_r=1",
              "calibration_note": config.get("calibration_note", "User-supplied nm/px; verify against microscope metadata"),
              "measurements": {"sem": sem["metrics"], "tem": tem["metrics"]},
              "quality":measurements["quality"],
              "image_preprocessing":{name:measurement.get("preprocess_metadata",{}) for name,measurement in (("sem",sem),("tem",tem))},
              "calibration":{name:{"nm_per_pixel_x":measurement.get("scale_x_nm",measurement["nm_per_pixel"]),
                                   "nm_per_pixel_y":measurement.get("scale_y_nm",measurement["nm_per_pixel"]),
                                   "source":config.get("calibration",{}).get(name,{}).get("source","configured_value")} for name,measurement in (("sem",sem),("tem",tem))},
              "tem_profiles":tem["per_ridge"],"active_intervals_nm":geometry.get("active_intervals_nm"),
              "geometry_assumptions": geometry.get("assumptions", []),
              "mesh": mesh_info,
              "grid": {"shape_xyz": list(grid["material_id"].shape), "cell_centers": True, "axis_order": "xyz", "units": "nm", "bounds_nm": grid["bounds_nm"], "actual_spacing_nm": grid["spacing_nm"], "requested_spacing_nm": grid["requested_spacing_nm"]},
              "materials": {ident: {"name": name, "at_wavelength": library.evaluate(name, [wavelength]), "source_metadata": library.metadata(name), "voxel_count": int(np.count_nonzero(grid["material_id"] == ident))} for ident, name in grid["materials"].items()},
              "regions": grid["regions"], "warnings": list(dict.fromkeys(warnings)), "files": files}
    if generated:
        # Only evaluate truth after measurement and geometry are finalized.
        truth = json.loads(Path(generated["truth_path"]).read_text(encoding="utf-8"))
        report["synthetic_truth"] = truth
        summary = generated.get("summary", {})
        expected = summary.get("pitch_nm", config.get("synthetic", {}).get("pitch_nm", 300))
        measured = sem["metrics"].get("pitch_mean_nm", sem.get("pitch_nm"))
        height_true = summary.get("height_nm", config.get("synthetic", {}).get("height_nm", 216))
        report["demo_validation"] = {"pitch_true_nm": expected, "pitch_measured_nm": measured, "pitch_error_nm": float(np.mean(measured)) - float(expected), "height_true_nm": height_true, "height_measured_nm": tem["height_nm"], "height_error_nm": tem["height_nm"] - height_true}
        for label, actual, true_key in (("sem_top_cd", sem["metrics"]["top_cd_mean_nm"], "top_cd_mean_nm"),
                                        ("tem_bottom_cd", tem["metrics"]["bottom_cd_mean_nm"], "tem_bottom_cd_mean_nm")):
            report["demo_validation"].update({label + "_true_nm": summary[true_key], label + "_measured_nm": actual,
                                               label + "_error_nm": actual - summary[true_key]})
    save_json(output / "config_used.json", config)
    _check_cancel(cancel_event)
    save_json(output / "report.json", report)
    log(f"Completed: {output}")
    return {"output_dir": str(output), "report": report, "files": files, "geometry": geometry, "grid": grid}
