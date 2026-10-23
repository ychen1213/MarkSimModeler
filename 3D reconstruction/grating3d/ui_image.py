"""Mouse-based image preparation for the desktop reconstruction interface.

ROI coordinates refer to the source file. All other annotations and overlays
refer to the cropped, rotated image, exactly as ``preprocess_image`` does.
"""
from __future__ import annotations

import math
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np
from PIL import Image, ImageTk

from .preprocessing import preprocess_image, read_image


class ImageCanvas(ttk.Frame):
    def __init__(self, parent, on_change=None, kind="SEM"):
        super().__init__(parent)
        self.on_change = on_change
        self.kind = kind.upper()
        self.path = None
        self.source_image = self.processed_image = None
        self.metadata = {}
        self.settings = {"rotation_deg": 0.0}
        self.calibration = {"sx": None, "sy": None, "source": "unconfirmed", "confirmed": False}
        self.overlay = None
        self._photo = None
        self._zoom, self._offset_x, self._offset_y = 1.0, 0.0, 0.0
        self._auto_fit = True
        self._canvas_size = None
        self._drag = self._pan = None
        self._points = []
        self._base_points = None
        self._suggested_angle = None
        self.rotation_var = tk.StringVar(value="0")
        self.tool = tk.StringVar(value="pan")
        self.view = tk.StringVar(value="processed")
        self.info = tk.StringVar(value="导入图像后，先圈选结构，再点击标尺两端完成校准。")
        self._build()

    def _build(self):
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 3))
        items = [("平移", "pan"), ("圈选结构", "roi"), ("点选标尺", "ruler"), ("排除区域", "exclude")]
        if self.kind == "TEM":
            items += [("基底两点", "base"), ("结构顶面", "top")]
        for label, key in items:
            ttk.Radiobutton(toolbar, text=label, value=key, variable=self.tool,
                            command=self._tool_changed).pack(side="left", padx=(0, 6))
        controls = ttk.Frame(self)
        controls.pack(fill="x", pady=(0, 4))
        ttk.Button(controls, text="适合窗口", command=self.fit_image).pack(side="left")
        ttk.Radiobutton(controls, text="原图 / 选 ROI", value="source", variable=self.view,
                        command=self._view_changed).pack(side="left", padx=(8, 4))
        ttk.Radiobutton(controls, text="裁切与旋转结果", value="processed", variable=self.view,
                        command=self._view_changed).pack(side="left", padx=(0, 8))
        angles = ttk.Frame(self)
        angles.pack(fill="x", pady=(0, 4))
        ttk.Label(angles, text="旋转 °").pack(side="left")
        rotation = ttk.Entry(angles, textvariable=self.rotation_var, width=7)
        rotation.pack(side="left", padx=4)
        rotation.bind("<Return>", lambda _: self.apply_rotation())
        ttk.Button(angles, text="应用", command=self.apply_rotation).pack(side="left")
        ttk.Button(angles, text="清除标注", command=self.clear_annotations).pack(side="right")
        if self.kind == "TEM":
            self.level_button = ttk.Button(angles, text="水平化基底", command=self.level_substrate, state="disabled")
            self.level_button.pack(side="left", padx=5)
        self.canvas = tk.Canvas(self, background="#182633", highlightthickness=0, width=500, height=270)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._configured)
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", lambda e: self._zoom_at(e.x, e.y, 1.2))
        self.canvas.bind("<Button-5>", lambda e: self._zoom_at(e.x, e.y, 1 / 1.2))
        self.canvas.bind("<ButtonPress-2>", self._start_pan)
        self.canvas.bind("<B2-Motion>", self._move_pan)
        self.canvas.bind("<ButtonPress-3>", self._start_pan)
        self.canvas.bind("<B3-Motion>", self._move_pan)
        self.info_label = ttk.Label(self, textvariable=self.info, wraplength=520)
        self.info_label.pack(side="bottom", before=self.canvas, fill="x", pady=(4, 0))

    def set_image(self, path, rotation_deg=0, reset=True):
        image, metadata = read_image(path)
        self.path = str(Path(path).resolve())
        self.source_image, self.metadata = image, metadata
        self.overlay = None
        if reset:
            self.settings = {"rotation_deg": float(rotation_deg)}
            self.calibration = {"sx": None, "sy": None, "source": "unconfirmed", "confirmed": False}
        else:
            self.settings["rotation_deg"] = float(rotation_deg)
        self.rotation_var.set(f"{float(rotation_deg):g}")
        self._points = []
        self._base_points = None
        self._suggested_angle = None
        if self.kind == "TEM":
            self.level_button.configure(state="disabled")
        self.view.set("processed")
        self._prepare()
        self.fit_image()
        depth = metadata.get("bit_depth", 8)
        self.info.set(f"{Path(path).name} · {image.shape[1]} × {image.shape[0]} px · {depth}-bit；滚轮缩放，右键拖动平移。")
        self._notify()

    def set_settings(self, config):
        keep = ("roi_px", "rotation_deg", "exclude_regions_px", "substrate_row_px", "top_row_px", "substrate_line_px")
        self.settings = {key: config[key] for key in keep if key in config and config[key] is not None}
        self.settings.setdefault("rotation_deg", 0.0)
        self.rotation_var.set(f"{float(self.settings['rotation_deg']):g}")
        self.overlay = None
        self._base_points = None
        self._suggested_angle = None
        if self.path:
            self._prepare()
            self.fit_image()

    def get_settings(self):
        import copy
        return copy.deepcopy(self.settings)

    def set_calibration(self, sx, sy=None, source="manual"):
        sy = sx if sy is None else sy
        sx, sy = float(sx), float(sy)
        if not np.isfinite([sx, sy]).all() or min(sx, sy) <= 0:
            raise ValueError("标尺必须是正数，单位 nm/pixel。")
        self.calibration = {"sx": sx, "sy": sy, "source": source, "confirmed": True}
        self.info.set(f"已校准：横向 {sx:g} nm/px，纵向 {sy:g} nm/px。")

    def get_calibration(self):
        result = dict(self.calibration)
        result.update(nm_per_pixel_x=result["sx"], nm_per_pixel_y=result["sy"])
        return result

    def clear_calibration(self):
        """Discard stale confirmation without notifying the host GUI again."""
        self.calibration = {"sx": None, "sy": None, "source": "unconfirmed", "confirmed": False}
        self.info.set("标尺已修改，请重新确认横、纵方向的实际比例。")

    def set_overlay(self, measurement):
        self.overlay = measurement
        if measurement is None:
            self.info.set("测量尚未更新，请重新提取。")
            self._render()
            return
        if measurement is not None and "display_image" in measurement:
            self.processed_image = np.asarray(measurement["display_image"], dtype=float)
            self.processed_metadata = measurement.get("preprocess_metadata", self.processed_metadata)
        self.view.set("processed")
        self.info.set("绿 / 橙实线为已测边缘；黄色虚线为插值；结构缺失处不连线。滚轮缩放核对边缘。")
        self._render()

    def clear_annotations(self):
        self.settings = {"rotation_deg": self.settings.get("rotation_deg", 0.0)}
        self.overlay = None
        self._points = []
        self._base_points = None
        self._suggested_angle = None
        if self.kind == "TEM":
            self.level_button.configure(state="disabled")
        if self.path:
            self._prepare()
            self.fit_image()
        self.info.set("已清除裁切和界面标注；标尺保留。")
        self._notify()

    def apply_rotation(self, degrees=None):
        try:
            value = float(self.rotation_var.get() if degrees is None else degrees)
            if not np.isfinite(value):
                raise ValueError
        except (ValueError, TypeError):
            messagebox.showerror("旋转角度", "请输入有效角度，例如 -3 或 2.5。", parent=self)
            return
        if not self.path:
            return
        self.settings["rotation_deg"] = value
        self.rotation_var.set(f"{value:g}")
        self._clear_processed_annotations()
        self._prepare()
        self.view.set("processed")
        self.fit_image()
        self.info.set("已应用旋转；请在当前预览上重新标注基底、顶面和排除区域。")
        self._notify()

    def _clear_processed_annotations(self):
        for key in ("substrate_row_px", "top_row_px", "substrate_line_px", "exclude_regions_px"):
            self.settings.pop(key, None)
        self.overlay = None
        self._points = []
        self._base_points = None
        self._suggested_angle = None
        if self.kind == "TEM":
            self.level_button.configure(state="disabled")

    def _prepare(self):
        self.processed_image, self.valid_mask, self.processed_metadata = preprocess_image(self.path, self.settings)

    def _current_image(self):
        return self.source_image if self.view.get() == "source" else self.processed_image

    def fit_image(self):
        self._auto_fit = True
        image = self._current_image()
        if image is None:
            return
        width, height = max(100, self.canvas.winfo_width()), max(100, self.canvas.winfo_height())
        self._zoom = min((width - 16) / image.shape[1], (height - 16) / image.shape[0])
        self._offset_x = (width - image.shape[1] * self._zoom) / 2
        self._offset_y = (height - image.shape[0] * self._zoom) / 2
        self._render()

    def image_to_canvas(self, x, y):
        return (self._offset_x + x * self._zoom, self._offset_y + y * self._zoom)

    def canvas_to_image(self, x, y, clamp=True):
        px, py = (x - self._offset_x) / self._zoom, (y - self._offset_y) / self._zoom
        image = self._current_image()
        if clamp and image is not None:
            px, py = np.clip(px, 0, image.shape[1]), np.clip(py, 0, image.shape[0])
        return float(px), float(py)

    def _configured(self, event):
        self.info_label.configure(wraplength=max(200, event.width - 12))
        old_size = self._canvas_size
        self._canvas_size = (event.width, event.height)
        if self.path and (self._auto_fit or self._photo is None):
            self.fit_image()
        else:
            # Keep the user's viewed centre stationary when the viewport
            # changes after a manual zoom or pan.
            if old_size is not None:
                self._offset_x += (event.width - old_size[0]) / 2
                self._offset_y += (event.height - old_size[1]) / 2
            self._render()

    def _view_changed(self):
        self._points = []
        self.fit_image()

    def _tool_changed(self):
        self._points = []
        previous_view = self.view.get()
        self.canvas.configure(cursor="hand2" if self.tool.get() == "pan" else "crosshair")
        if self.tool.get() == "roi":
            self.view.set("source")
            self.info.set("在原图拖出矩形，保留完整光栅边缘，避开文字、标尺及保护层。")
        elif self.tool.get() in {"top", "base", "exclude"}:
            self.view.set("processed")
            instructions = {"top": "点击目标光栅的顶面；制样保护层应位于所选顶面以上。",
                            "base": "点击基底界面的两个相距较远的点；斜线将给出水平化建议。",
                            "exclude": "拖出矩形排除文字、污染或不可用图像区域。"}
            self.info.set(instructions[self.tool.get()])
        elif self.tool.get() == "ruler":
            self.info.set("点击已知长度标尺的两个端点；可分别校准横、纵方向。")
        if previous_view != self.view.get():
            self.fit_image()
        else:
            self._render()

    def _press(self, event):
        if self.path is None:
            return
        mode = self.tool.get()
        if mode == "pan":
            self._start_pan(event)
            return
        point = self.canvas_to_image(event.x, event.y)
        if mode in {"roi", "exclude"}:
            self._drag = point
        elif mode == "top":
            self.settings["top_row_px"] = point[1]
            self.overlay = None
            self.info.set(f"目标顶面：当前处理图 y = {point[1]:.1f} px。")
            self._notify()
        elif mode in {"ruler", "base"}:
            self._points.append(point)
            if len(self._points) == 2:
                points, self._points = self._points, []
                if mode == "ruler":
                    self._calibrate_ruler(points)
                else:
                    self._set_base(points)
        self._render()

    def _motion(self, event):
        if self.tool.get() == "pan":
            self._move_pan(event)
        elif self._drag is not None:
            self._render()
            self._rectangle((*self._drag, *self.canvas_to_image(event.x, event.y)), "#ffd06a", dash=(5, 3))

    def _release(self, event):
        self._pan = None
        if self._drag is None:
            return
        start, end = self._drag, self.canvas_to_image(event.x, event.y)
        self._drag = None
        x0, y0 = np.floor(np.minimum(start, end)).astype(int)
        x1, y1 = np.ceil(np.maximum(start, end)).astype(int)
        if x1 - x0 < 3 or y1 - y0 < 3:
            self._render()
            return
        rectangle = list(map(int, [x0, y0, x1, y1]))
        if self.tool.get() == "roi":
            self.settings["roi_px"] = rectangle
            self._clear_processed_annotations()
            self._prepare()
            self.info.set(f"已选择原图 ROI：{rectangle}。选择‘裁切与旋转结果’核对处理图。")
        else:
            self.settings.setdefault("exclude_regions_px", []).append(rectangle)
            self.overlay = None
            self.info.set(f"已排除 {len(self.settings['exclude_regions_px'])} 个区域。")
        self._render()
        self._notify()

    def _set_base(self, points):
        first, second = points
        dx, dy = second[0] - first[0], second[1] - first[1]
        if abs(dx) < 3:
            self.info.set("基底两点的横向距离过小，请重新选择。")
            return
        self._base_points = points
        self._suggested_angle = math.degrees(math.atan(dy / dx))
        self.overlay = None
        if abs(self._suggested_angle) <= 0.5:
            self.settings["substrate_row_px"] = (first[1] + second[1]) / 2
            self.info.set(f"已标注基底 y = {self.settings['substrate_row_px']:.1f} px；倾斜 {self._suggested_angle:+.2f}°。")
        else:
            self.settings.pop("substrate_row_px", None)
            self.info.set(f"基底倾斜 {self._suggested_angle:+.2f}°；点击‘水平化基底’应用补偿，然后标注顶面。")
        self.level_button.configure(state="normal")
        self._notify()

    def level_substrate(self):
        if not self._base_points or self._suggested_angle is None:
            return
        first, second = self._base_points
        angle = self._suggested_angle
        height, width = self.processed_image.shape
        cx, cy = width / 2, height / 2
        px, py = (first[0] + second[0]) / 2, (first[1] + second[1]) / 2
        row = -math.sin(math.radians(angle)) * (px - cx) + math.cos(math.radians(angle)) * (py - cy) + cy
        self.apply_rotation(float(self.settings.get("rotation_deg", 0)) + angle)
        self.settings["substrate_row_px"] = float(row)
        self.info.set(f"已水平化基底；基底 y = {row:.1f} px。请在当前图重新标注目标顶面。")
        self._render()
        self._notify()

    def _calibrate_ruler(self, points):
        dx, dy = abs(points[1][0] - points[0][0]), abs(points[1][1] - points[0][1])
        distance = math.hypot(dx, dy)
        if distance < 2:
            self.info.set("标尺两点太近，请放大图像后重新选择。")
            return
        sx, sy = self.calibration["sx"], self.calibration["sy"]
        rectangular = sx is not None and sy is not None and not math.isclose(sx, sy, rel_tol=1e-6)
        default_axis = ("x" if dx >= dy else "y") if rectangular else "both"
        dialog = _RulerDialog(self, dx, dy, default_axis)
        if dialog.result is None:
            return
        length, axis = dialog.result
        pixels = {"x": dx, "y": dy, "both": distance}[axis]
        if pixels < 2:
            messagebox.showerror("校准", "所选方向上的两点距离太小。", parent=self)
            return
        scale = length / pixels
        if axis in {"x", "both"}:
            self.calibration["sx"] = scale
        if axis in {"y", "both"}:
            self.calibration["sy"] = scale
        self.calibration["source"] = "scale_bar"
        self.calibration["confirmed"] = all(v is not None and v > 0 for v in (self.calibration["sx"], self.calibration["sy"]))
        sx, sy = self.calibration["sx"], self.calibration["sy"]
        self.info.set(f"标尺校准：横向 {f'{sx:g}' if sx else '待校准'}，纵向 {f'{sy:g}' if sy else '待校准'} nm/px。")
        self._notify()

    def _start_pan(self, event):
        self._auto_fit = False
        self._pan = (event.x, event.y, self._offset_x, self._offset_y)

    def _move_pan(self, event):
        if self._pan:
            x, y, ox, oy = self._pan
            self._offset_x, self._offset_y = ox + event.x - x, oy + event.y - y
            self._render()

    def _wheel(self, event):
        self._zoom_at(event.x, event.y, 1.2 if event.delta > 0 else 1 / 1.2)

    def _zoom_at(self, x, y, factor):
        if self.path is None:
            return
        self._auto_fit = False
        px, py = self.canvas_to_image(x, y, clamp=False)
        self._zoom = max(0.02, min(32.0, self._zoom * factor))
        self._offset_x, self._offset_y = x - px * self._zoom, y - py * self._zoom
        self._render()

    def _render(self):
        self.canvas.delete("all")
        image = self._current_image()
        if image is None:
            self.canvas.create_text(20, 25, text="请选择 SEM / TEM 图像", anchor="nw", fill="#ccd9e4")
            return
        # Resize only the visible image portion, keeping zoomed images bounded.
        cw, ch = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        x0 = max(0, int(math.floor(-self._offset_x / self._zoom)))
        y0 = max(0, int(math.floor(-self._offset_y / self._zoom)))
        x1 = min(image.shape[1], int(math.ceil((cw - self._offset_x) / self._zoom)))
        y1 = min(image.shape[0], int(math.ceil((ch - self._offset_y) / self._zoom)))
        if x1 > x0 and y1 > y0:
            crop = Image.fromarray(np.clip(image[y0:y1, x0:x1], 0, 255).astype(np.uint8))
            crop = crop.resize((max(1, round((x1 - x0) * self._zoom)), max(1, round((y1 - y0) * self._zoom))), Image.Resampling.BILINEAR)
            self._photo = ImageTk.PhotoImage(crop, master=self.canvas)
            self.canvas.create_image(*self.image_to_canvas(x0, y0), anchor="nw", image=self._photo)
        if self.view.get() == "source":
            if self.settings.get("roi_px"):
                self._rectangle(self.settings["roi_px"], "#ffd06a")
        else:
            for rect in self.settings.get("exclude_regions_px", []):
                self._rectangle(rect, "#ff8a8a", dash=(4, 3))
            for key, color in (("substrate_row_px", "#4ad6ef"), ("top_row_px", "#8aeb7f")):
                if key in self.settings:
                    self._line([(0, self.settings[key]), (image.shape[1], self.settings[key])], color, dash=(6, 3))
            if self._base_points:
                self._line(self._base_points, "#4ad6ef", width=2)
            if self.overlay:
                self._draw_overlay(self.overlay)
        for point in self._points:
            cx, cy = self.image_to_canvas(*point)
            self.canvas.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill="#ffd06a", outline="white")

    def _rectangle(self, rect, color, dash=None):
        self.canvas.create_rectangle(*self.image_to_canvas(rect[0], rect[1]), *self.image_to_canvas(rect[2], rect[3]),
                                     outline=color, width=2, dash=dash)

    def _line(self, points, color, width=1.5, dash=None):
        coords = [coordinate for point in points for coordinate in self.image_to_canvas(*point)]
        if len(coords) >= 4:
            self.canvas.create_line(*coords, fill=color, width=width, dash=dash)

    def _draw_trace(self, x, y, active=None, color="#57e2bb", dash=None):
        x, y = np.asarray(x, float), np.asarray(y, float)
        usable = np.isfinite(x) & np.isfinite(y)
        if active is not None:
            usable &= np.asarray(active, bool)
        transitions = np.diff(np.r_[False, usable, False].astype(int))
        for start, end in zip(np.flatnonzero(transitions == 1), np.flatnonzero(transitions == -1)):
            if end - start >= 2:
                step = max(1, (end - start) // 800)
                indices = np.unique(np.r_[np.arange(start, end, step), end - 1])
                self._line(list(zip(x[indices], y[indices])), color, dash=dash)

    def _draw_overlay(self, measurement):
        sx = float(measurement.get("scale_x_nm", measurement.get("nm_per_pixel_x", measurement.get("nm_per_pixel", 1))))
        sy = float(measurement.get("scale_y_nm", measurement.get("nm_per_pixel_y", sx)))
        if "y_nm" in measurement:
            y = np.asarray(measurement["y_nm"]) / sy
            active = measurement.get("ridge_active")
            observed = measurement.get("edge_valid_mask")
            interpolated = measurement.get("edge_interpolated_mask")
            for index, (left, right) in enumerate(zip(measurement["left_nm"], measurement["right_nm"])):
                present = None if active is None else np.asarray(active)[index]
                measured = present
                if observed is not None:
                    measured = np.asarray(observed)[index].copy()
                    if present is not None:
                        measured &= present
                self._draw_trace(np.asarray(left) / sx, y, measured)
                self._draw_trace(np.asarray(right) / sx, y, measured, color="#ffb860")
                if interpolated is not None:
                    filled = np.asarray(interpolated)[index].copy()
                    # Include adjacent measured endpoints so even a single
                    # interpolated sample is visible as a dashed segment.
                    expanded = filled.copy()
                    expanded[1:] |= filled[:-1]
                    expanded[:-1] |= filled[1:]
                    if present is not None:
                        expanded &= present
                    self._draw_trace(np.asarray(left) / sx, y, expanded, color="#ffd06a", dash=(5, 3))
                    self._draw_trace(np.asarray(right) / sx, y, expanded, color="#ffd06a", dash=(5, 3))
        else:
            image = self.processed_image
            if "substrate_row_px" in measurement:
                row = measurement["substrate_row_px"]
                self._line([(0, row), (image.shape[1], row)], "#4ad6ef")
            for ridge, trace in zip(measurement.get("per_ridge", []), measurement.get("sidewall_traces", [])):
                y = np.asarray(trace["rows_px"])
                self._draw_trace(np.asarray(trace["left_nm"]) / sx, y)
                self._draw_trace(np.asarray(trace["right_nm"]) / sx, y, color="#ffb860")
                if "top_row_px" in ridge:
                    self._line([(0, ridge["top_row_px"]), (image.shape[1], ridge["top_row_px"])], "#8aeb7f", dash=(3, 5))

    def _notify(self):
        if self.on_change:
            self.on_change()


class _RulerDialog(tk.Toplevel):
    def __init__(self, parent, dx, dy, default_axis):
        super().__init__(parent)
        self.result = None
        self.title("输入标尺的实际长度")
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"两点距离：横向 {dx:.1f} px，纵向 {dy:.1f} px").pack(anchor="w")
        ttk.Label(frame, text="实际长度（nm）").pack(anchor="w", pady=(10, 4))
        self.length = tk.StringVar(value="100")
        entry = ttk.Entry(frame, textvariable=self.length, width=30)
        entry.pack(fill="x")
        self.axis = tk.StringVar(value=default_axis)
        ttk.Label(frame, text="校准方向").pack(anchor="w", pady=(10, 4))
        for label, value in [("横纵相同（方形像素）", "both"), ("仅横向 X", "x"), ("仅纵向 Y", "y")]:
            ttk.Radiobutton(frame, text=label, value=value, variable=self.axis).pack(anchor="w")
        ttk.Label(frame, text="矩形像素请选择单一方向，并分别校准 X、Y。", wraplength=330).pack(anchor="w", pady=10)
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Button(row, text="采用标尺", command=self.accept).pack(side="right")
        ttk.Button(row, text="取消", command=self.destroy).pack(side="right", padx=8)
        self.bind("<Return>", lambda _: self.accept())
        self.bind("<Escape>", lambda _: self.destroy())
        self.grab_set()
        entry.focus_set()
        entry.selection_range(0, "end")
        self.wait_window()

    def accept(self):
        try:
            length = float(self.length.get())
            if not math.isfinite(length) or length <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("标尺", "实际长度必须是正数。", parent=self)
            return
        self.result = length, self.axis.get()
        self.destroy()
