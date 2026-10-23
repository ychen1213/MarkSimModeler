"""Calibrated SEM top-view and TEM cross-section edge measurements.

All physical lengths are nanometres. Image sample positions are pixel centres,
and the supplied nm/pixel values are calibration inputs, never inferred from a
synthetic truth file. These routines assume approximately vertical, separated
grating lines and a horizontal substrate in the TEM image.
"""
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, gaussian_filter1d, median_filter
from scipy.signal import find_peaks

from .preprocessing import read_image, preprocess_image, calibration_scales


def _image(path, config):
    display, pixel_valid, metadata = preprocess_image(path, config)
    image = display.copy()
    polarity = config.get("polarity", "dark")
    if polarity not in {"dark", "bright"}:
        raise ValueError("polarity must be dark or bright")
    if polarity == "bright":
        image = 255.0 - image
    return image, display, pixel_valid, metadata


def _calibration(value):
    return calibration_scales(value)[0]


def _otsu(values):
    values = np.asarray(values, dtype=float)
    lo, hi = np.percentile(values, [0.5, 99.5])
    if hi - lo < 5:
        raise ValueError("Insufficient image contrast to identify edges")
    hist, boundaries = np.histogram(values, bins=256, range=(lo, hi))
    centres = (boundaries[:-1] + boundaries[1:]) / 2
    weights = np.cumsum(hist)
    moments = np.cumsum(hist * centres)
    denom = weights * (weights[-1] - weights)
    variance = np.zeros_like(denom, dtype=float)
    valid = denom > 0
    variance[valid] = (moments[-1] * weights[valid] - moments[valid] * weights[-1]) ** 2 / denom[valid]
    return float(centres[np.argmax(variance)])


def _runs(mask, minimum=3):
    changes = np.diff(np.r_[False, np.asarray(mask, dtype=bool), False].astype(int))
    starts, ends = np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)
    return [(int(a), int(b)) for a, b in zip(starts, ends) if b - a >= minimum]


def _crossing(profile, a, b, threshold):
    """Threshold crossing between two samples, in pixel-centre coordinates."""
    va, vb = profile[a], profile[b]
    fraction = 0.5 if abs(vb - va) < 1e-12 else (threshold - va) / (vb - va)
    return float(a + 0.5 + np.clip(fraction, 0.0, 1.0) * (b - a))


def _edges_for_run(profile, run, threshold):
    a, b = run
    if a == 0 or b == len(profile):
        return None
    return _crossing(profile, a - 1, a, threshold), _crossing(profile, b - 1, b, threshold)


def _local_midpoint(profile, run, bounds):
    a, b = run
    lo, hi = bounds
    interior = profile[a + max(0, (b - a) // 4):b - max(0, (b - a) // 4)]
    exterior = np.r_[profile[lo:max(lo, a - 2)], profile[min(hi, b + 2):hi]]
    if len(interior) < 2 or len(exterior) < 2:
        return None
    dark, light = np.median(interior), np.median(exterior)
    if light - dark < 5:
        return None
    return float((dark + light) / 2)


def _smooth_valid(image, valid, sigma):
    if valid.all():
        return gaussian_filter(image, sigma=sigma)
    denominator = gaussian_filter(valid.astype(float), sigma=sigma)
    return gaussian_filter(image * valid, sigma=sigma) / np.maximum(denominator, 1e-12)


def _rim_position(profile, lo, hi, expected, minimum_prominence):
    """Centre of a bright band; a contrast feature, not a proven material edge."""
    segment = profile[lo:hi]
    peaks, _ = find_peaks(segment, prominence=minimum_prominence)
    if not len(peaks):
        return None
    peak = int(min(peaks, key=lambda p: abs(p + lo + .5 - expected)))
    peak_level = segment[peak]
    # Background and ridge interior can have very different intensities. Use a
    # separate half-contrast threshold on each side of the band, otherwise its
    # brightest plateau shifts towards the brighter neighboring population.
    left_floor = float(np.percentile(segment[:peak], 15)) if peak > 0 else peak_level
    right_floor = float(np.percentile(segment[peak + 1:], 15)) if peak + 1 < len(segment) else peak_level
    left_cut, right_cut = (peak_level + left_floor) / 2, (peak_level + right_floor) / 2
    a = b = peak
    while a > 0 and segment[a - 1] >= left_cut:
        a -= 1
    while b < len(segment) - 1 and segment[b + 1] >= right_cut:
        b += 1
    if a == 0 or b == len(segment) - 1:
        return None
    band_left = _crossing(profile, lo + a - 1, lo + a, left_cut)
    band_right = _crossing(profile, lo + b, lo + b + 1, right_cut)
    return float((band_left + band_right) / 2)


def _missing_rows(left, right, pixel_valid, config):
    """Fill reference contours while retaining absent-row topology separately."""
    raw_valid = np.isfinite(left) & np.isfinite(right)
    policy = config.get("missing_policy", "preserve")
    if policy not in {"preserve", "reject", "interpolate"}:
        raise ValueError("missing_policy must be preserve, reject or interpolate")
    if policy == "reject" and not raw_valid.all():
        raise ValueError("SEM contains missing edge rows; inspect the image or choose an explicit missing_policy")
    active, interpolated = raw_valid.copy(), np.zeros(raw_valid.shape, dtype=bool)
    smoothing = float(config.get("edge_smooth_rows", 1.0))
    if not np.isfinite(smoothing) or smoothing < 0:
        raise ValueError("edge_smooth_rows must be finite and nonnegative")
    max_gap = int(config.get("max_interpolate_gap_px", 2))
    if not 0 <= max_gap <= 2 and policy == "preserve":
        raise ValueError("preserve policy only permits bounded short gaps of at most 2 pixels")
    rows = np.arange(left.shape[1])
    gaps = []
    for ridge in range(len(left)):
        valid = raw_valid[ridge]
        if not valid.any():
            raise ValueError(f"SEM ridge {ridge} has no usable measured edges")
        for start, end in _runs(~valid, minimum=1):
            bounded = start > 0 and end < len(rows) and valid[start - 1] and valid[end]
            short = bounded and end - start <= max_gap
            if policy == "interpolate" or short:
                active[ridge, start:end] = True
                interpolated[ridge, start:end] = True
            gaps.append({"ridge_index": ridge, "start_row_px": start, "end_row_px": end,
                         "row_count": end - start, "interpolated": bool(policy == "interpolate" or short),
                         "reason": "unmeasured: specimen gap or acquisition failure; not distinguishable automatically"})
        # These finite contours are references only where ridge_active is false.
        left[ridge] = np.interp(rows, rows[valid], left[ridge, valid])
        right[ridge] = np.interp(rows, rows[valid], right[ridge, valid])
        if smoothing > 0:
            for start, end in _runs(active[ridge], minimum=1):
                left[ridge, start:end] = gaussian_filter1d(left[ridge, start:end], smoothing)
                right[ridge, start:end] = gaussian_filter1d(right[ridge, start:end], smoothing)
    return raw_valid, active, interpolated, gaps


def extract_sem(path, nm_per_pixel, config=None):
    """Measure separated ridge edges in the processed image coordinate system.

    Long missing stretches are absent geometry by default. ``interpolate`` is
    an explicit acquisition-repair assumption; it can erase genuine defects.
    Bright-rim pairing measures the centre of each bright band and requires
    the user to validate that those centres represent the intended boundary.
    """
    config = config or {}
    sx, sy = calibration_scales(nm_per_pixel)
    image, display, pixel_valid, metadata = _image(path, config)
    if metadata["rotation_deg"] and not np.isclose(sx, sy):
        raise ValueError("Rotation with unequal x/y calibration is ambiguous; first resample to equal physical pixel sizes, then rotate")
    sigma = float(config.get("smooth_sigma_px", 0.8))
    if not np.isfinite(sigma) or sigma < 0:
        raise ValueError("smooth_sigma_px must be finite and nonnegative")
    edge_mode = config.get("edge_mode", "threshold")
    if edge_mode not in {"threshold", "bright_rim"}:
        raise ValueError("edge_mode must be threshold or bright_rim")
    smoothed = _smooth_valid(display if edge_mode == "bright_rim" else image, pixel_valid, sigma)
    h, w = image.shape
    start, end = int(.1 * h), max(int(.9 * h), int(.1 * h) + 1)
    counts = pixel_valid[start:end].sum(axis=0)
    projection = (smoothed[start:end] * pixel_valid[start:end]).sum(axis=0) / np.maximum(counts, 1)
    # Fill columns entirely outside the valid footprint only for seed finding.
    if np.any(counts == 0):
        known = np.flatnonzero(counts)
        if len(known) < 4:
            raise ValueError("SEM ROI has too few usable pixels")
        projection = np.interp(np.arange(w), known, projection[known])
    minimum = int(config.get("min_width_px", 4))
    warnings = list(metadata["warnings"])
    if edge_mode == "bright_rim":
        prominence = float(config.get("rim_prominence", max(5, .12 * np.ptp(projection))))
        peaks, _ = find_peaks(projection, prominence=prominence, distance=max(2, minimum))
        if len(peaks) < 2 or len(peaks) % 2:
            raise ValueError("Bright rims could not be paired unambiguously; crop complete pairs or use threshold mode")
        seeds = [(int(a), int(b) + 1) for a, b in zip(peaks[::2], peaks[1::2])]
        warnings.append("Bright-rim centres are contrast features: inspect peak pairing and validate the physical edge definition")
    else:
        global_threshold = float(config["threshold"]) if "threshold" in config else _otsu(projection)
        seeds = _runs(projection < global_threshold, minimum)
    if not seeds:
        raise ValueError("No separated SEM ridges found; check polarity, ROI and calibration")
    if any(a == 0 or b == w for a, b in seeds):
        raise ValueError("SEM ROI cuts through a ridge; include both edges of each measured ridge")
    if edge_mode == "threshold":
        bright_peaks, _ = find_peaks(projection, prominence=max(8, .12 * np.ptp(projection)), distance=max(2, minimum))
        if len(bright_peaks) >= 2 * len(seeds):
            warnings.append("SEM has prominent bright bands: threshold edges may follow the inner rim; inspect bright-rim mode and confirm physical boundaries")
    centres = np.array([(a + b) / 2 for a, b in seeds])
    bounds = np.r_[0, np.round((centres[:-1] + centres[1:]) / 2).astype(int), w]
    left, right = np.full((len(seeds), h), np.nan), np.full((len(seeds), h), np.nan)
    for row in range(h):
        profile = smoothed[row]
        for ridge, seed in enumerate(seeds):
            lo, hi = int(bounds[ridge]), int(bounds[ridge + 1])
            if edge_mode == "bright_rim":
                middle = int(centres[ridge])
                le = _rim_position(profile, lo, middle, seed[0] + .5, max(5, prominence / 2))
                re = _rim_position(profile, middle, hi, seed[1] - .5, max(5, prominence / 2))
                edges = (le, re) if le is not None and re is not None else None
            else:
                midpoint = _local_midpoint(profile, seed, (lo, hi))
                if midpoint is None:
                    continue
                candidates = [(a + lo, b + lo) for a, b in _runs(profile[lo:hi] < midpoint, minimum)]
                if not candidates:
                    continue
                run = min(candidates, key=lambda ab: abs((ab[0] + ab[1]) / 2 - centres[ridge]))
                edges = _edges_for_run(profile, run, midpoint)
            if edges is not None and edges[1] > edges[0]:
                a, b = max(0, int(np.floor(edges[0] - .5))), min(w, int(np.ceil(edges[1] + .5)))
                if a < b and pixel_valid[row, a:b].all():
                    left[ridge, row], right[ridge, row] = edges
    raw_valid = np.isfinite(left) & np.isfinite(right)
    fractions = raw_valid.mean(axis=1)
    minimum_fraction = float(config.get("min_valid_fraction", .8))
    if not 0 < minimum_fraction <= 1:
        raise ValueError("min_valid_fraction must lie in (0,1]")
    if np.any(fractions < minimum_fraction):
        raise ValueError(f"SEM edge tracking coverage is insufficient: {fractions.tolist()}; inspect ROI, defects and edge mode")
    raw_left, raw_right = left.copy() * sx, right.copy() * sx
    raw_valid, active, interpolated, gaps = _missing_rows(left, right, pixel_valid, config)
    left, right = left * sx, right * sx
    cd = right - left
    ridge_centres = np.array([np.mean((a[m] + b[m]) / 2) for a, b, m in zip(left, right, active)])
    pitches = np.diff(ridge_centres)
    y = (np.arange(h) + .5) * sy
    detrended, waviness, slopes = [], [], []
    for a, b, mask in zip(left, right, active):
        fit_a, fit_b = np.polyfit(y[mask], a[mask], 1), np.polyfit(y[mask], b[mask], 1)
        detrended.append(float(np.sqrt((np.mean((a[mask] - np.polyval(fit_a, y[mask])) ** 2)
                                       + np.mean((b[mask] - np.polyval(fit_b, y[mask])) ** 2)) / 2)))
        waviness.append(float(np.sqrt((np.var(a[mask]) + np.var(b[mask])) / 2)))
        slopes.append([float(fit_a[0]), float(fit_b[0])])
    if np.any(~active):
        warnings.append("Long unmeasured SEM stretches are omitted from 3D geometry; confirm whether they are defects or acquisition loss")
    if config.get("missing_policy", "preserve") == "interpolate" and gaps:
        warnings.append("All missing SEM rows were explicitly interpolated; genuine missing structure may have been filled")
    if interpolated.any():
        warnings.append("Interpolated edges are flagged separately from measured edges")
    metrics = {"ridge_count": len(seeds), "pitch_nm": float(np.mean(pitches)) if len(pitches) else None,
               "pitch_std_nm": float(np.std(pitches)) if len(pitches) else None,
               "top_cd_mean_nm": float(np.mean(cd[active])), "top_cd_std_nm": float(np.std(cd[active])),
               "edge_valid_fraction": fractions.tolist(), "edge_ler_rms_nm": detrended,
               "edge_raw_waviness_rms_nm": waviness, "edge_linear_slope": slopes,
               "edge_interpolated_fraction": interpolated.mean(axis=1).tolist(),
               "ridge_absent_fraction": (~active).mean(axis=1).tolist()}
    return {"source_path": str(Path(path).resolve()), "image_shape": tuple(metadata["original_shape"]),
            "roi_shape": image.shape, "nm_per_pixel": sx, "scale_x_nm": sx, "scale_y_nm": sy,
            "y_nm": y, "left_nm": left, "right_nm": right, "centers_nm": ridge_centres,
            "top_cd_nm": np.array([np.mean(row[mask]) for row, mask in zip(cd, active)]),
            "pitch_nm": metrics["pitch_nm"], "raw_left_nm": raw_left, "raw_right_nm": raw_right,
            "ridge_active": active, "edge_valid_mask": raw_valid, "edge_interpolated_mask": interpolated,
            "edge_invalid_mask": ~raw_valid, "valid_pixel_mask": pixel_valid,
            "display_image": display, "preprocess_metadata": metadata,
            "diagnostics": {"missing_policy": config.get("missing_policy", "preserve"), "missing_runs": gaps,
                            "edge_mode": edge_mode, "ler_definition": "linear-detrended edge RMS; includes imaging noise and waviness"},
            "metrics": metrics, "warnings": warnings,
            "assumptions": ["Separated ridges approximately parallel to the processed image y axis",
                            "SEM contrast boundaries require physical interpretation; calibration supplied by the user",
                            "Missing geometry is omitted at all depths under the extrusion assumption"]}


def extract_tem(path, nm_per_pixel, config=None):
    """Measure ridge heights and fit linear/quadratic walls above a substrate.

    Sidewall angles are measured from the substrate plane: 90 degrees is
    vertical. Both bottom offsets are positive for outward widening. A negative
    offset describes an undercut. ``substrate_row_px`` and ``top_row_px`` can
    override automatic interface detection in processed pixel-centre coordinates.
    Explicit top markers exclude protective caps, whose chemistry cannot be
    inferred from image gray values.
    """
    config = config or {}
    sx, sy = calibration_scales(nm_per_pixel)
    image, display, pixel_valid, metadata = _image(path, config)
    if metadata["rotation_deg"] and not np.isclose(sx, sy):
        raise ValueError("TEM leveling with unequal x/y calibration is ambiguous; first resample to equal physical pixel sizes, then rotate")
    sigma = float(config.get("smooth_sigma_px", .7))
    if not np.isfinite(sigma) or sigma < 0:
        raise ValueError("smooth_sigma_px must be finite and nonnegative")
    smoothed = _smooth_valid(image, pixel_valid, sigma)
    model = config.get("sidewall_model", "linear")
    if model not in {"linear", "quadratic"}:
        raise ValueError("sidewall_model must be linear or quadratic")
    warnings = list(metadata["warnings"])
    h, w = image.shape
    # Use the bright background population: ridge coverage can exceed 50% near
    # the bottom, so a row median can jump at a sidewall rather than substrate.
    visible = np.where(pixel_valid, smoothed, np.nan)
    with np.errstate(invalid="ignore"):
        row_signal = np.nanpercentile(visible, 85, axis=1)
    usable_rows = np.flatnonzero(np.isfinite(row_signal))
    if len(usable_rows) < 6:
        raise ValueError("Cannot locate TEM substrate: too few valid image rows")
    row_signal = gaussian_filter1d(np.interp(np.arange(h), usable_rows, row_signal[usable_rows]), .5)
    if "substrate_row_px" in metadata:
        substrate = float(metadata["substrate_row_px"])
    elif "substrate_row_px" in config:
        substrate = float(config["substrate_row_px"])
    else:
        derivative = np.gradient(row_signal)
        candidates = np.arange(max(2, h // 4), h - 3)
        boundary_index = int(candidates[np.argmin(derivative[candidates])])
        if derivative[boundary_index] > -2:
            raise ValueError("Cannot locate a horizontal TEM substrate boundary; supply substrate_row_px")
        air = float(np.median(row_signal[max(0, boundary_index - 12):max(1, boundary_index - 3)]))
        substrate_level = float(np.median(row_signal[boundary_index + 3:min(h, boundary_index + 12)]))
        midpoint = (air + substrate_level) / 2
        crossings = [i for i in range(max(0, boundary_index - 3), min(h - 1, boundary_index + 4))
                     if row_signal[i] >= midpoint > row_signal[i + 1]]
        substrate = (_crossing(row_signal, crossings[0], crossings[0] + 1, midpoint)
                     if crossings else boundary_index + 0.5)
    if not (5 < substrate < h - 1):
        raise ValueError("TEM substrate boundary must be inside the image below the ridge tops")
    above = visible[:max(1, int(substrate - 2))]
    column_signal = np.nanpercentile(above, 8, axis=0)
    usable_columns = np.flatnonzero(np.isfinite(column_signal))
    if len(usable_columns) < 4:
        raise ValueError("No TEM ridges found in valid image pixels")
    column_signal = np.interp(np.arange(w), usable_columns, column_signal[usable_columns])
    threshold = float(config["threshold"]) if "threshold" in config else _otsu(column_signal)
    seeds = _runs(column_signal < threshold, int(config.get("min_width_px", 4)))
    if not seeds:
        raise ValueError("No TEM ridges found above the substrate")
    centres = np.array([(a + b) / 2 for a, b in seeds])
    bounds = np.r_[0, np.round((centres[:-1] + centres[1:]) / 2).astype(int), w]
    top_override = config.get("top_row_px")
    if top_override is not None:
        tops = np.asarray(top_override, dtype=float)
        if tops.ndim == 0:
            tops = np.repeat(tops, len(seeds))
        if tops.shape != (len(seeds),) or not np.isfinite(tops).all():
            raise ValueError("top_row_px must be a finite scalar or one marker per TEM ridge")
        warnings.append("TEM mark tops are user supplied; layers above those markers are excluded from ridge geometry")
    else:
        tops = None
    ridges = []
    traces = []
    for ridge, (a, b) in enumerate(seeds):
        central_left, central_right = int(a + 0.35 * (b - a)), int(b - 0.35 * (b - a))
        vertical = np.mean(smoothed[:, central_left:max(central_left + 1, central_right)], axis=1)
        dark = float(np.percentile(vertical[:int(substrate - 3)], 10))
        air = float(np.percentile(vertical[:int(substrate - 3)], 90))
        if air - dark < 10:
            raise ValueError(f"TEM ridge {ridge} has insufficient top contrast")
        midpoint = (air + dark) / 2
        if tops is not None:
            top = float(tops[ridge])
        else:
            top_candidates = [i for i in range(0, max(0, int(substrate - 5)))
                              if vertical[i] >= midpoint > vertical[i + 1]]
            if not top_candidates:
                raise ValueError(f"Cannot identify top surface of TEM ridge {ridge}; mark top_row_px manually")
            persistent = [i for i in top_candidates if np.mean(vertical[i + 1:min(int(substrate - 2), i + 6)] < midpoint) > 0.7]
            top_index = persistent[0] if persistent else top_candidates[0]
            top = _crossing(vertical, top_index, top_index + 1, midpoint)
        height = substrate - top
        if not 0 <= top < substrate or height < 5:
            raise ValueError(f"TEM ridge {ridge} is too short to fit sidewalls")
        rows = np.arange(int(np.ceil(top + max(2.0, 0.08 * height))), int(np.floor(substrate - max(2.0, 0.06 * height))))
        left_edge, right_edge, valid_rows = [], [], []
        lo, hi = int(bounds[ridge]), int(bounds[ridge + 1])
        for row in rows:
            profile = smoothed[row]
            local_threshold = _local_midpoint(profile, (a, b), (lo, hi))
            if local_threshold is None:
                continue
            candidates = [(u + lo, v + lo) for u, v in _runs(profile[lo:hi] < local_threshold, 3)]
            if not candidates:
                continue
            run = min(candidates, key=lambda uv: abs((uv[0] + uv[1]) / 2 - centres[ridge]))
            edges = _edges_for_run(profile, run, local_threshold)
            if edges is not None and pixel_valid[row, max(0, int(edges[0] - 1)):min(w, int(edges[1] + 1))].all():
                left_edge.append(edges[0]); right_edge.append(edges[1]); valid_rows.append(row + 0.5)
        required_fraction = float(config.get("min_valid_fraction", .8))
        if not 0 < required_fraction <= 1:
            raise ValueError("TEM min_valid_fraction must lie in (0,1]")
        if len(valid_rows) < 4 or len(valid_rows) / max(len(rows), 1) < required_fraction:
            raise ValueError(f"Not enough usable TEM sidewall samples for ridge {ridge}; inspect internal layers, exclusions and contrast")
        valid_rows = np.asarray(valid_rows)
        left_edge, right_edge = np.asarray(left_edge), np.asarray(right_edge)
        # Fit relative depth: numerically stable and directly usable by 3D geometry.
        degree = 1 if model == "linear" else 2
        depth_rows = (valid_rows - top) / height
        left_fit = np.polyfit(depth_rows, median_filter(left_edge, size=3), degree)
        right_fit = np.polyfit(depth_rows, median_filter(right_edge, size=3), degree)
        left_top, right_top = np.polyval(left_fit, 0), np.polyval(right_fit, 0)
        left_bottom, right_bottom = np.polyval(left_fit, 1), np.polyval(right_fit, 1)
        left_outward, right_outward = left_top - left_bottom, right_bottom - right_top
        depth_samples = np.linspace(0, 1, int(config.get("profile_samples", 33)))
        if len(depth_samples) < 3:
            raise ValueError("profile_samples must be at least 3")
        profile = {"depth_fraction": depth_samples.tolist(),
                   "left_outward_nm": ((left_top - np.polyval(left_fit, depth_samples)) * sx).tolist(),
                   "right_outward_nm": ((np.polyval(right_fit, depth_samples) - right_top) * sx).tolist(),
                   "measured_depth_range": [float(depth_rows.min()), float(depth_rows.max())],
                   "fit_model": model,
                   "endpoint_extrapolation": True}
        entry = {"index": ridge, "center_nm": float(((left_top + right_top) / 2) * sx),
                 "top_row_px": float(top), "height_nm": float(height * sy),
                 "top_cd_nm": float((right_top - left_top) * sx),
                 "bottom_cd_nm": float((right_bottom - left_bottom) * sx),
                 "left_top_nm": float(left_top * sx), "right_top_nm": float(right_top * sx),
                 "left_outward_nm": float(left_outward * sx),
                 "right_outward_nm": float(right_outward * sx),
                 "left_angle_deg": float(np.degrees(np.arctan2(height * sy, left_outward * sx))),
                 "right_angle_deg": float(np.degrees(np.arctan2(height * sy, right_outward * sx))),
                 "left_fit_rms_nm": float(np.sqrt(np.mean((left_edge - np.polyval(left_fit, depth_rows)) ** 2)) * sx),
                 "right_fit_rms_nm": float(np.sqrt(np.mean((right_edge - np.polyval(right_fit, depth_rows)) ** 2)) * sx),
                 "sidewall_profile": profile, "sidewall_model": model,
                 "sidewall_valid_fraction": float(len(valid_rows) / max(len(rows), 1))}
        ridges.append(entry)
        traces.append({"rows_px": valid_rows, "left_nm": left_edge * sx,
                       "right_nm": right_edge * sx})
    def average(key):
        return float(np.mean([r[key] for r in ridges]))
    metrics = {"ridge_count": len(ridges), "height_mean_nm": average("height_nm"),
               "top_cd_mean_nm": average("top_cd_nm"), "bottom_cd_mean_nm": average("bottom_cd_nm"),
               "left_angle_mean_deg": average("left_angle_deg"), "right_angle_mean_deg": average("right_angle_deg")}
    metrics["height_std_nm"] = float(np.std([r["height_nm"] for r in ridges]))
    metrics["sidewall_fit_rms_mean_nm"] = float(np.mean([r[k] for r in ridges for k in ("left_fit_rms_nm", "right_fit_rms_nm")]))
    for r in ridges:
        if max(r["left_fit_rms_nm"], r["right_fit_rms_nm"]) > float(config.get("fit_rms_warning_nm", 2)):
            warnings.append(f"TEM ridge {r['index']} sidewall residual exceeds the review limit; inspect the chosen {model} model")
        if r["sidewall_valid_fraction"] < .9:
            warnings.append(f"TEM ridge {r['index']} has unmeasured sidewall samples; possible internal interfaces or image corruption")
    if metrics["height_std_nm"] > max(2 * sy, .03 * metrics["height_mean_nm"]):
        warnings.append("TEM heights vary significantly across ridges: inspect substrate tilt, top markers and section geometry")
    if tops is None:
        warnings.append("Automatically found TEM tops may include protective caps; confirm the actual mark interface")
    return {"source_path": str(Path(path).resolve()), "image_shape": tuple(metadata["original_shape"]),
            "roi_shape": image.shape, "nm_per_pixel": sx, "scale_x_nm": sx, "scale_y_nm": sy,
            "substrate_row_px": float(substrate),
            "height_nm": metrics["height_mean_nm"], "per_ridge": ridges,
            "sidewall_traces": traces, "metrics": metrics, "warnings": warnings,
            "display_image": display, "valid_pixel_mask": pixel_valid, "preprocess_metadata": metadata,
            "diagnostics": {"sidewall_model": model, "manual_top": tops is not None,
                            "manual_substrate": "substrate_row_px" in config or "substrate_line_px" in config},
            "assumptions": [f"Planar substrate and {model} sidewall fit in the observed section",
                            "Sidewall angles measured from the substrate horizontal plane",
                            "TEM section-to-SEM correspondence supplied by ordered or median matching"]}


def plot_edge_overlays(sem, tem, output_dir):
    """Save inspectable measured-edge overlays without changing source images."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, measurement in (("sem", sem), ("tem", tem)):
        image = measurement.get("display_image")
        if image is None:
            image, _ = read_image(measurement["source_path"])
        fig, ax = plt.subplots(figsize=(12, 5 if name == "sem" else 3))
        ax.imshow(image, cmap="gray", vmin=0, vmax=255)
        sx = measurement.get("scale_x_nm", measurement["nm_per_pixel"])
        sy = measurement.get("scale_y_nm", measurement["nm_per_pixel"])
        pixel_valid = measurement.get("valid_pixel_mask")
        if pixel_valid is not None and not np.all(pixel_valid):
            tint = np.zeros((*np.asarray(image).shape, 4))
            tint[..., 0] = 1
            tint[..., 3] = np.where(pixel_valid, 0, .23)
            ax.imshow(tint)
        if name == "sem":
            for index, (left, right) in enumerate(zip(sem["left_nm"], sem["right_nm"])):
                active = sem.get("ridge_active", np.ones(np.asarray(sem["left_nm"]).shape, dtype=bool))[index]
                interpolated = sem.get("edge_interpolated_mask", np.zeros(np.asarray(sem["left_nm"]).shape, dtype=bool))[index]
                y = sem["y_nm"] / sy - .5
                for edge, colour in ((left, "#33d2d2"), (right, "#ff9f33")):
                    ax.plot(np.where(active & ~interpolated, edge / sx - .5, np.nan), y, color=colour, linewidth=.8)
                    ax.plot(np.where(interpolated, edge / sx - .5, np.nan), y, color="#ee70ed", marker=".", markersize=2, linewidth=.8)
                    ax.plot(np.where(~active, edge / sx - .5, np.nan), y, color="#ff4b4b", linestyle=":", linewidth=1)
            ax.text(.01, .02, "Measured: cyan/orange | interpolated: magenta | absent (reference): red dotted", transform=ax.transAxes,
                    color="white", fontsize=8, bbox={"facecolor": "black", "alpha": .6, "pad": 3})
        else:
            ax.axhline(tem["substrate_row_px"] - 0.5, color="#33d2d2", linewidth=1)
            for ridge, trace in zip(tem["per_ridge"], tem["sidewall_traces"]):
                ax.plot(trace["left_nm"] / sx - 0.5, trace["rows_px"] - 0.5, color="#ff9f33", linewidth=0.8)
                ax.plot(trace["right_nm"] / sx - 0.5, trace["rows_px"] - 0.5, color="#ff9f33", linewidth=0.8)
                profile = ridge.get("sidewall_profile")
                if profile:
                    depth = np.asarray(profile["depth_fraction"])
                    y = ridge["top_row_px"] + depth * ridge["height_nm"] / sy - .5
                    ax.plot((ridge["left_top_nm"] - np.asarray(profile["left_outward_nm"])) / sx - .5, y,
                            color="#ee70ed", linestyle="--", linewidth=.8)
                    ax.plot((ridge["right_top_nm"] + np.asarray(profile["right_outward_nm"])) / sx - .5, y,
                            color="#ee70ed", linestyle="--", linewidth=.8)
                ax.axhline(ridge["top_row_px"] - 0.5, color="#72e484", alpha=0.35, linewidth=0.6)
        ax.set_title(f"{name.upper()} measured edges | x {sx:g}, y {sy:g} nm/pixel | processed coordinates")
        ax.set_axis_off()
        fig.tight_layout()
        path = output_dir / f"{name}_edges.png"
        fig.savefig(path, dpi=140, bbox_inches="tight")
        plt.close(fig)
        paths[name + "_edges"] = str(path)
    return paths
