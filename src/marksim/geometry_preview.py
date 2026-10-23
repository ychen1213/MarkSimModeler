"""Presentation of saved geometry, grouped by its originating process step.

This module never changes coordinates or materials. In particular, the top
view shows opaque visible surfaces, and cross-sections keep any real gaps.
All views share ``geometry_display.step_groups`` with the process/preview UI.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
from html import escape
import json
import math
from pathlib import Path
import textwrap

import numpy as np

from .geometry_display import step_groups

PREVIEW_VERSION = "process-step-preview-1.2"
PREVIEW_MANIFEST = "geometry_preview.json"
_PREVIEW_FILES = ("geometry_3d.html", "geometry_sections.png")


def _geometry_digest(geometry):
    """Fingerprint the original exchange object without normalizing it in place."""
    encoded = json.dumps(geometry, sort_keys=True, ensure_ascii=False,
                         allow_nan=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def preview_matches(geometry, folder):
    """Whether these view artifacts use current colors and the exact source.

    Historical images used material colors and have no display manifest. They
    cannot be shown beside a new step-colored table without regeneration into
    a separate view folder. File hashes also reject interrupted/stale exports.
    This check only reads; it never edits source geometry or historical assets.
    """
    folder = Path(folder)
    try:
        manifest = json.loads((folder / PREVIEW_MANIFEST).read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            return False
        if (manifest.get("schema_version") != "1.0"
                or manifest.get("display_version") != PREVIEW_VERSION
                or manifest.get("geometry_sha256") != _geometry_digest(geometry)):
            return False
        files = manifest.get("files")
        if not isinstance(files, dict):
            return False
        # Only inspect the two known local names; paths inside a manifest are
        # never followed to an arbitrary location.
        return all(
            isinstance(files.get(name), dict)
            and (folder / name).is_file()
            and files[name].get("sha256") == hashlib.sha256((folder / name).read_bytes()).hexdigest()
            for name in _PREVIEW_FILES
        )
    except (OSError, ValueError, TypeError):
        return False


@lru_cache(maxsize=1)
def _font_family():
    """Use an installed CJK font without changing or downloading any fonts."""
    from matplotlib import font_manager
    available = {font.name for font in font_manager.fontManager.ttflist}
    return next((font for font in (
        "Microsoft YaHei", "Microsoft YaHei UI", "Noto Sans CJK SC",
        "SimHei", "WenQuanYi Zen Hei", "Arial Unicode MS",
    ) if font in available), "DejaVu Sans")


def section_intervals(polygon, axis, cut):
    """Intersect an xy polygon with x/y = cut using half-open edge tests.

    ``axis=0`` returns x intervals at the given y; ``axis=1`` returns y
    intervals at the given x. Opposite crossings delimit material, so a
    concave cross-section can contain multiple separate intervals.
    """
    polygon = np.asarray(polygon, dtype=float)
    other = 1 - axis
    crossings = []
    for a, b in zip(polygon, np.roll(polygon, -1, axis=0)):
        if (a[other] <= cut < b[other]) or (b[other] <= cut < a[other]):
            fraction = (cut - a[other]) / (b[other] - a[other])
            crossings.append(float(a[axis] + fraction * (b[axis] - a[axis])))
    crossings.sort()
    return [(lo, hi) for lo, hi in zip(crossings[::2], crossings[1::2]) if hi > lo]


def _legend_label(group):
    label = group["label"]
    material = group.get("material") or "—"
    status = f" · {group['status']}" if not group["solids"] else ""
    # Break long step names rather than clipping the legend outside the image.
    name_lines = textwrap.wrap(label, width=38, break_long_words=True) or [label]
    material_lines = textwrap.wrap(f"材料：{material}{status}", width=40,
                                 break_long_words=True)
    return "\n".join(name_lines + material_lines)


def _surface_limits(groups, background, full_limits):
    """Zoom only the repeated panel to the recorded non-substrate features."""
    features = [
        solid for group in groups if group["operation"] != "create_substrate"
        for solid in group["solids"] if solid["material"] != background
    ]
    if not features:
        return list(full_limits), False
    bottom = min(solid["z_min_um"] for solid in features)
    top = max(solid["z_max_um"] for solid in features)
    padding = max((top - bottom) * .15, 1e-6)
    limits = [max(full_limits[0], bottom - padding),
              min(full_limits[1], top + padding)]
    zoom = limits[1] - limits[0] < .7 * (full_limits[1] - full_limits[0])
    return (limits if zoom else list(full_limits)), zoom


def build_section_figure(geometry):
    """Build inspectable top/xz/yz/three-period xz views and a shared legend."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch, Polygon, Rectangle

    groups = step_groups(geometry)
    labels = [_legend_label(group) for group in groups]
    legend_columns = 2 if len(groups) > 1 else 1
    legend_rows = max(1, math.ceil(len(groups) / legend_columns))
    max_label_lines = max((label.count("\n") + 1 for label in labels), default=1)
    legend_height = .35 + legend_rows * (.21 * max_label_lines + .17)
    figure_height = 7.0 + legend_height
    with matplotlib.rc_context({
        "font.family": _font_family(), "font.size": 10,
        "axes.unicode_minus": False, "axes.titlesize": 12,
        "axes.titleweight": "bold", "axes.labelsize": 10,
        "axes.edgecolor": "#9aaabd", "text.color": "#25364a",
        "axes.labelcolor": "#40556d", "xtick.color": "#40556d",
        "ytick.color": "#40556d",
    }):
        fig, axes = plt.subplots(2, 2, figsize=(13, figure_height))
        bx, by, bz = np.asarray(geometry["cell_bounds_um"], float)
        cut_y, cut_x = float(np.mean(by)), float(np.mean(bx))
        period = float(geometry["period_x_um"])
        entries = [(group, solid) for group in groups for solid in group["solids"]]

        # Paint lower surfaces first. Opaque higher surfaces correctly obscure
        # them: a full cap appears as a full cap instead of mixed material colors.
        for group, solid in sorted(entries, key=lambda entry: entry[1]["z_max_um"]):
            polygon = np.asarray(solid["polygon_xy_um"], float)
            axes[0, 0].add_patch(Polygon(
                polygon, facecolor=group["color"], edgecolor="#40556d",
                linewidth=.45, alpha=1.0,
            ))

        # Boolean partition edges are implementation details, not material
        # interfaces. Omit per-piece outlines so adjacent same-step slices
        # appear continuous; different steps remain identifiable by color.
        for group, solid in entries:
            polygon = np.asarray(solid["polygon_xy_um"], float)
            bottom, top = float(solid["z_min_um"]), float(solid["z_max_um"])
            for ax, axis, cut in (
                (axes[0, 1], 0, cut_y), (axes[1, 0], 1, cut_x),
            ):
                for lo, hi in section_intervals(polygon, axis, cut):
                    ax.add_patch(Rectangle(
                        (lo, bottom), hi - lo, top - bottom,
                        facecolor=group["color"], edgecolor="none",
                        linewidth=0, antialiased=False, alpha=1.0,
                    ))
            # Repeat the actual central x-z section, including the substrate.
            # This keeps films and vertical contact visible in all three cells.
            for lo, hi in section_intervals(polygon, 0, cut_y):
                for repeat in (-1, 0, 1):
                    axes[1, 1].add_patch(Rectangle(
                        (lo + repeat * period, bottom), hi - lo, top - bottom,
                        facecolor=group["color"], edgecolor="none",
                        linewidth=0, antialiased=False, alpha=1.0,
                    ))

        axes[0, 0].set(xlim=bx, ylim=by, title="俯视：从上方可见的表面",
                       xlabel="x / μm", ylabel="y / μm")
        axes[0, 1].set(xlim=bx, ylim=bz, title=f"x-z 截面（y = {cut_y:g} μm）",
                       xlabel="x / μm", ylabel="z / μm")
        axes[1, 0].set(xlim=by, ylim=bz, title=f"y-z 截面（x = {cut_x:g} μm）",
                       xlabel="y / μm", ylabel="z / μm")
        surface_limits, zoomed = _surface_limits(
            groups, geometry.get("background_material"), bz,
        )
        axes[1, 1].set(xlim=[bx[0] - period, bx[1] + period], ylim=surface_limits,
                       title="三个母周期 · x-z " + ("表面局部放大" if zoomed else "截面"),
                       xlabel="x / μm", ylabel="z / μm")
        for boundary in bx:
            axes[1, 1].axvline(boundary, color="#25364a", linestyle="--",
                               linewidth=.9, alpha=.8)
        for ax in axes.flat:
            ax.set_facecolor("#f7f9fc")
            ax.tick_params(labelsize=9, length=3)
            ax.set_axisbelow(True)
            ax.grid(alpha=.16, linewidth=.5)

        handles = [Patch(
            facecolor=group["color"], edgecolor="#40556d",
            alpha=1 if group["solids"] else .4,
            hatch=None if group["solids"] else "//",
            label=label,
        ) for group, label in zip(groups, labels)]
        if handles:
            fig.legend(handles=handles, loc="lower center", ncol=legend_columns,
                       bbox_to_anchor=(.5, .035), frameon=False,
                       title="工艺步骤与颜色（同一步骤的所有实体使用同一颜色）",
                       fontsize=9.5, title_fontsize=10.5,
                       handlelength=1.6, handleheight=1.5,
                       labelspacing=1.1, columnspacing=2.8)
        background = geometry.get("background_material", "未记录")
        fig.suptitle("几何预览 · 按工艺步骤着色", fontsize=15, fontweight="bold", y=.985)
        fig.text(.5, .945,
                 "完整覆膜会遮住下层俯视图；请在截面查看内部。各面板独立缩放，尺寸以坐标为准。",
                 ha="center", fontsize=10, color="#52677f")
        fig.text(.5, .012, f"空白区域：背景材料 {background}　｜　虚线：中央母周期边界",
                 ha="center", fontsize=9, color="#52677f")
        fig.subplots_adjust(left=.07, right=.98, top=.89,
                            bottom=(legend_height + .45) / figure_height,
                            wspace=.22, hspace=.45)
    return fig


def _mesh(solid):
    """Triangulate a vertical primitive without moving any of its vertices."""
    polygon = np.asarray(solid["polygon_xy_um"], float)
    n = len(polygon)
    vertices = np.vstack([
        np.column_stack([polygon, np.full(n, solid[key])])
        for key in ("z_min_um", "z_max_um")
    ])
    # Geometry's vertical Boolean decomposition supplies convex, hole-free
    # trapezoids/triangles. Their top and bottom faces admit this triangle fan.
    faces = []
    for j in range(1, n - 1):
        faces.extend([(0, j + 1, j), (n, n + j, n + j + 1)])
    for j in range(n):
        k = (j + 1) % n
        faces.extend([(j, k, n + k), (j, n + k, n + j)])
    return vertices, np.asarray(faces)


def build_3d_figure(geometry):
    """One legend entry/toggle per process step, with per-piece hover data."""
    import plotly.graph_objects as go

    fig = go.Figure()
    for group in step_groups(geometry):
        name = f"{group['label']} · {group.get('material') or '—'}"
        if not group["solids"]:
            # An empty trace retains disabled/etched/future steps in the
            # legend without adding invented geometry to the scene.
            fig.add_trace(go.Scatter3d(
                x=[None], y=[None], z=[None], mode="markers",
                marker=dict(color=group["color"], size=7, symbol="square"),
                name=escape(f"{name} · {group['status']}"),
                legendgroup=group["key"], showlegend=True,
                hoverinfo="skip",
            ))
        for index, solid in enumerate(group["solids"]):
            vertices, faces = _mesh(solid)
            hover = (
                f"步骤：{escape(group['label'])}<br>"
                f"材料：{escape(str(solid['material']))}<br>"
                f"实体：{escape(str(solid.get('name', index + 1)))}<br>"
                f"该步骤的第 {index + 1} / {len(group['solids'])} 片<br>"
                f"z：{solid['z_min_um']:g} ～ {solid['z_max_um']:g} μm"
                "<br>x = %{x:.4g} μm<br>y = %{y:.4g} μm<br>z = %{z:.4g} μm"
                "<extra></extra>"
            )
            fig.add_trace(go.Mesh3d(
                x=vertices[:, 0], y=vertices[:, 1], z=vertices[:, 2],
                i=faces[:, 0], j=faces[:, 1], k=faces[:, 2],
                color=group["color"], opacity=1,
                name=escape(name), legendgroup=group["key"],
                showlegend=index == 0, hovertemplate=hover,
                flatshading=True,
                lighting=dict(ambient=.75, diffuse=.5, specular=.1),
            ))
    family = geometry.get("mark", {}).get("mark_type", "自定义 / 既有几何")
    fig.update_layout(
        title=dict(text=f"{escape(str(family))} · 按工艺步骤着色"
                        "<br><sup>点击图例可整组显示 / 隐藏；隐藏上层后可检查内部结构。</sup>",
                   x=.03),
        font=dict(family="Microsoft YaHei, Microsoft YaHei UI, Arial, sans-serif",
                  color="#25364a"),
        paper_bgcolor="#ffffff",
        legend=dict(groupclick="togglegroup", itemclick="toggle",
                    itemdoubleclick="toggleothers", x=0, y=-.06,
                    xanchor="left", yanchor="top", orientation="h",
                    title=dict(text="工艺步骤 · 材料")),
        margin=dict(l=0, r=0, t=85, b=110),
        scene=dict(xaxis_title="x / μm", yaxis_title="y / μm", zaxis_title="z / μm",
                   aspectmode="data", bgcolor="#f7f9fc"),
    )
    return fig


def preview_geometry(geometry, folder):
    """Write the established offline PNG/HTML artifacts without editing data."""
    import matplotlib.pyplot as plt

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    html_path, section_path = folder / "geometry_3d.html", folder / "geometry_sections.png"
    build_3d_figure(geometry).write_html(html_path, include_plotlyjs=True)
    figure = build_section_figure(geometry)
    try:
        figure.savefig(section_path, dpi=150, facecolor="white")
    finally:
        plt.close(figure)
    # Publish this display-only manifest last. An interrupted export cannot be
    # mistaken for a matching set, even when older files exist in the folder.
    manifest = {
        "schema_version": "1.0", "display_version": PREVIEW_VERSION,
        "geometry_sha256": _geometry_digest(geometry),
        "files": {
            name: {"sha256": hashlib.sha256((folder / name).read_bytes()).hexdigest()}
            for name in _PREVIEW_FILES
        },
    }
    (folder / PREVIEW_MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8",
    )
    return {"html": str(html_path), "sections": str(section_path)}
