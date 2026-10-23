"""Non-destructive microscope image loading, display normalization and ROI transforms.

The returned images use a processed pixel coordinate system whose origin is the
cropped, rotated image's upper-left corner. Source transforms remain in metadata.
"""
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import rotate


def read_image(path):
    """Return ``(display_gray_float_0_255, metadata)`` without 16-bit saturation.

    Native samples are read before normalization. 8-bit gray values remain
    unchanged; high-bit and float samples use a documented finite percentile
    window. The original file is preserved and can always be reopened to access
    its native quantitative samples. This normalization is for edge processing,
    not a physical conversion of electron counts.
    """
    with Image.open(Path(path)) as source:
        raw = np.asarray(source).copy()
        mode = source.mode
        if raw.ndim == 3 or mode in {"P", "1"}:
            raw = np.asarray(source.convert("L")).copy()
    if raw.ndim != 2 or min(raw.shape) < 2:
        raise ValueError("Image must contain a two-dimensional grayscale field")
    values = raw.astype(float)
    finite = np.isfinite(values)
    if not finite.any():
        raise ValueError("Image has no finite intensity samples")
    lo_raw, hi_raw = map(float, [values[finite].min(), values[finite].max()])
    identity = raw.dtype == np.uint8
    if identity:
        lo, hi = 0.0, 255.0
        display = values.copy()
    else:
        lo, hi = map(float, np.percentile(values[finite], [0.5, 99.5]))
        if hi <= lo:
            lo, hi = lo_raw, hi_raw
        display = (np.clip(values, lo, hi) - lo) * (255.0 / (hi - lo)) if hi > lo else np.full(values.shape, 127.5)
    display[~finite] = float(np.median(display[finite]))
    metadata = {"source_path": str(Path(path).resolve()), "raw_dtype": str(raw.dtype),
                "source_mode": mode, "bit_depth": int(raw.dtype.itemsize * 8),
                "raw_range": [lo_raw, hi_raw], "shape": list(raw.shape),
                "finite_pixel_fraction": float(finite.mean()),
                "invalid_pixel_count": int((~finite).sum()),
                "normalization": {"method": "identity_8bit" if identity else "percentile_window",
                                  "low": lo, "high": hi, "output_range": [0, 255]},
                "source_preserved": True}
    return display, metadata


def calibration_scales(value):
    """Accept scalar, ``[x,y]`` or ``{'x': x, 'y': y}`` nm/pixel calibration."""
    try:
        if isinstance(value, dict):
            scales = np.asarray([value["x"], value["y"]], dtype=float)
        else:
            scales = np.asarray(value, dtype=float)
            if scales.ndim == 0:
                scales = np.repeat(scales, 2)
        if scales.shape != (2,) or not np.isfinite(scales).all() or (scales <= 0).any():
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ValueError("nm_per_pixel calibration must be a positive finite scalar or x/y pair") from None
    return float(scales[0]), float(scales[1])


def preprocess_image(path, config=None):
    """Return display image, usable-pixel mask and source/transform metadata.

    ``roi_px`` is a source-image rectangle [x0,y0,x1,y1]. ``rotation_deg`` is
    scipy's counterclockwise rotation, performed after cropping, without canvas
    expansion. Rotated fill pixels are explicitly invalid.
    """
    config = config or {}
    image, metadata = read_image(path)
    original_shape = image.shape
    roi = config.get("roi_px") or [0, 0, image.shape[1], image.shape[0]]
    if len(roi) != 4 or any(not np.isfinite(float(v)) for v in roi):
        raise ValueError("roi_px must be [x0,y0,x1,y1] inside the image")
    x0, y0, x1, y1 = map(int, roi)
    if not (0 <= x0 < x1 <= image.shape[1] and 0 <= y0 < y1 <= image.shape[0]):
        raise ValueError("roi_px must be [x0,y0,x1,y1] inside the image")
    image = image[y0:y1, x0:x1].copy()
    valid = np.ones(image.shape, dtype=bool)
    if metadata["invalid_pixel_count"]:
        with Image.open(Path(path)) as source:
            native_finite = np.isfinite(np.asarray(source, dtype=float))
        valid = native_finite[y0:y1, x0:x1].copy()
    degrees = float(config.get("rotation_deg", 0))
    if not np.isfinite(degrees):
        raise ValueError("rotation_deg must be finite")
    warnings = []
    if metadata["invalid_pixel_count"]:
        warnings.append("Non-finite native intensity samples are excluded from edge measurement")
    # A user supplied substrate line is in cropped, pre-rotation pixel centres.
    # Rotating by atan(slope) makes its line horizontal in image-down coordinates.
    line = config.get("substrate_line_px")
    transformed_substrate = None
    if line is not None:
        line = np.asarray(line, dtype=float)
        if line.shape != (4,) or not np.isfinite(line).all() or abs(line[2] - line[0]) < 1:
            raise ValueError("substrate_line_px needs two finite, separated x/y points")
        if degrees != 0:
            raise ValueError("Use substrate_line_px or rotation_deg, not both for TEM leveling")
        degrees = float(np.degrees(np.arctan((line[3] - line[1]) / (line[2] - line[0]))))
        angle = np.deg2rad(degrees)
        cx, cy = image.shape[1] / 2, image.shape[0] / 2
        transformed_substrate = float(-np.sin(angle) * ((line[0] + line[2]) / 2 - cx)
                                      + np.cos(angle) * ((line[1] + line[3]) / 2 - cy) + cy)
    if degrees:
        image = rotate(image, degrees, reshape=False, order=1, mode="constant", cval=float(np.median(image)))
        valid = rotate(valid.astype(np.uint8), degrees, reshape=False, order=0, mode="constant", cval=0).astype(bool)
        warnings.append("Rotation fill pixels are excluded; coordinates refer to the processed image")
    for rectangle in config.get("exclude_regions_px", []):
        if len(rectangle) != 4 or not np.isfinite(np.asarray(rectangle, dtype=float)).all():
            raise ValueError("exclude_regions_px needs [x0,y0,x1,y1] rectangles")
        a, b, c, d = map(int, rectangle)
        if not (0 <= a < c <= image.shape[1] and 0 <= b < d <= image.shape[0]):
            raise ValueError("Excluded rectangles must be inside the processed image")
        valid[b:d, a:c] = False
    metadata.update({"original_shape": list(original_shape), "processed_shape": list(image.shape),
                     "roi_px": [x0, y0, x1, y1], "rotation_deg": degrees,
                     "coordinate_system": "processed_image_pixel_centres",
                     "valid_pixel_fraction": float(valid.mean()), "warnings": warnings})
    metadata["exclude_regions_px"] = config.get("exclude_regions_px", [])
    if transformed_substrate is not None:
        metadata["substrate_row_px"] = transformed_substrate
        metadata["substrate_line_px"] = line.tolist()
    return image, valid, metadata
