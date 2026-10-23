"""Create paired SEM/TEM test images from a known grating geometry."""
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, gaussian_filter1d


def generate_demo(output_dir, config=None):
    """Generate new demo images and a separate, validation-only truth file.

    The original SEM/TEM files are never touched. Rendering approximates edge
    blur and noise, not a physics-based electron microscope image formation model.
    """
    defaults = {"seed": 42, "n_ridges": 8, "pitch_nm": 300.0, "top_cd_nm": 150.0,
                "height_nm": 216.0, "bottom_outward_nm": 21.0,
                "sem_nm_per_pixel": 3.0, "tem_nm_per_pixel": 3.0,
                "sem_shape": (440, 920), "tem_shape": (195, 920),
                "edge_roughness_nm": 4.0, "noise_sigma": 4.0,
                "blur_sigma_px": 0.65, "bowing_nm": 0.0}
    settings = {**defaults, **(config or {})}
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(int(settings["seed"]))
    sem_scale, tem_scale = float(settings["sem_nm_per_pixel"]), float(settings["tem_nm_per_pixel"])
    sem_scale_y = float(settings.get("sem_nm_per_pixel_y",sem_scale))
    tem_scale_y = float(settings.get("tem_nm_per_pixel_y",tem_scale))
    if min(sem_scale, tem_scale,sem_scale_y,tem_scale_y) <= 0:
        raise ValueError("Synthetic image calibrations must be positive")
    h, w = map(int, settings["sem_shape"])
    th, tw = map(int, settings["tem_shape"])
    count = int(settings["n_ridges"])
    pitch, cd, height = (float(settings[key]) for key in ("pitch_nm", "top_cd_nm", "height_nm"))
    outward = float(settings["bottom_outward_nm"])
    bowing = float(settings["bowing_nm"])
    if not (count >= 1 and height > 0 and cd > 0 and cd + 2 * max(outward, 0) + 2 * max(bowing, 0) < pitch):
        raise ValueError("Synthetic ridges require positive dimensions and separated sidewalls")
    first = float(settings.get("first_center_nm", 0.75 * pitch))
    centres = first + np.arange(count) * pitch
    if centres[-1] + cd / 2 + outward >= min(w * sem_scale, tw * tem_scale):
        raise ValueError("Image width is too small for the requested grating")
    y = (np.arange(h) + 0.5) * sem_scale_y
    left, right = [], []
    roughness = float(settings["edge_roughness_nm"])
    for index, centre in enumerate(centres):
        drift = 2.5 * np.sin(2 * np.pi * y / (390 + 13 * index) + 0.4 * index)
        linewidth = cd + 4 * np.sin(2 * np.pi * y / (275 + 11 * index) + index)
        noise_left = gaussian_filter1d(rng.normal(size=h), 2.5)
        noise_right = gaussian_filter1d(rng.normal(size=h), 2.5)
        noise_left *= roughness / max(np.std(noise_left), 1e-12)
        noise_right *= roughness / max(np.std(noise_right), 1e-12)
        left.append(centre + drift - linewidth / 2 + noise_left)
        right.append(centre + drift + linewidth / 2 + noise_right)
    left, right = np.asarray(left), np.asarray(right)
    x = (np.arange(w) + 0.5) * sem_scale
    sem_mask = np.any((x[None, None, :] >= left[:, :, None]) & (x[None, None, :] <= right[:, :, None]), axis=0)
    sem = np.where(sem_mask, 64.0, 212.0)
    sem = gaussian_filter(sem, float(settings["blur_sigma_px"]))
    sem += rng.normal(0, float(settings["noise_sigma"]), sem.shape)
    # TEM samples the same y section used in the truth record.
    section = float(settings.get("tem_section_y_nm", h * sem_scale_y / 2))
    tl = np.array([np.interp(section, y, edge) for edge in left])
    tr = np.array([np.interp(section, y, edge) for edge in right])
    substrate = float(settings.get("substrate_row_px", th - 38))
    top = substrate - height / tem_scale_y
    if top < 5 or substrate >= th - 3:
        raise ValueError("TEM height does not fit above the substrate in the image")
    tx = (np.arange(tw) + 0.5) * tem_scale
    ty = np.arange(th) + 0.5
    depth = np.clip((ty - top) / (substrate - top), 0, 1)
    bow = 4 * bowing * depth * (1 - depth)
    lz = tl[:, None] - outward * depth[None, :] - bow[None, :]
    rz = tr[:, None] + outward * depth[None, :] + bow[None, :]
    tem_mask = np.any((tx[None, None, :] >= lz[:, :, None]) & (tx[None, None, :] <= rz[:, :, None]), axis=0)
    tem_mask &= ((ty >= top) & (ty < substrate))[:, None]
    tem = np.where(tem_mask, 56.0, 214.0)
    tem[ty >= substrate] = 136.0
    tem = gaussian_filter(tem, float(settings["blur_sigma_px"]))
    tem += rng.normal(0, float(settings["noise_sigma"]), tem.shape)
    sem_path, tem_path, truth_path = (output_dir / name for name in ("demo_sem.png", "demo_tem.png", "truth.json"))
    Image.fromarray(np.clip(sem, 0, 255).astype(np.uint8)).save(sem_path)
    Image.fromarray(np.clip(tem, 0, 255).astype(np.uint8)).save(tem_path)
    summary = {"ridge_count": count, "pitch_nm": pitch, "top_cd_mean_nm": float(np.mean(right - left)),
               "height_nm": height, "tem_top_cd_mean_nm": float(np.mean(tr - tl)),
               "tem_bottom_cd_mean_nm": float(np.mean(tr - tl) + 2 * outward),
               "left_outward_nm": outward, "right_outward_nm": outward,
               "sidewall_angle_deg": float(np.degrees(np.arctan2(height, outward))),
               "bowing_nm": bowing}
    truth = {"warning": "Validation only. Reconstruction must never read or infer parameters from this file.",
             "config": settings, "summary": summary, "sem_y_nm": y.tolist(),
             "sem_left_nm": left.tolist(), "sem_right_nm": right.tolist(),
             "tem_section_y_nm": section, "tem_left_top_nm": tl.tolist(), "tem_right_top_nm": tr.tolist(),
             "tem_top_row_px": top, "tem_substrate_row_px": substrate}
    truth_path.write_text(json.dumps(truth, indent=2), encoding="utf-8")
    return {"sem_path": str(sem_path), "tem_path": str(tem_path), "truth_path": str(truth_path),
            "config": settings, "summary": summary}
