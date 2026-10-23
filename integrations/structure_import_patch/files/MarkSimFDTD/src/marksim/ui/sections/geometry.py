"""Geometry object listing, section preview and saved snapshots."""
from __future__ import annotations
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk
import webbrowser
import numpy as np

from ... import service
from ...geometry_display import step_groups
from ...schemas import read_json, write_json
from ..widgets import ImageView


class GeometrySection:
    """Feature methods sharing the single MarkSimApp state owner."""

    def _geometry_page(self):
        page = self.pages[2]
        row = ttk.Frame(page); row.pack(fill="x")
        self.button(row, "打开可旋转 3D 视图", self.open_geometry_3d)
        self.button(row, "放大截面图", self.open_geometry_sections)
        self.button(row, "查看步骤快照", self.choose_geometry_snapshot)
        self.button(row, "编辑对应工艺步骤", self.edit_geometry_step)
        self.geometry_description = tk.StringVar(value="尚未生成几何。A 入口直接从 GDS 与工艺生成；B 入口为 SEM/TEM 重建。")
        ttk.Label(page, textvariable=self.geometry_description, wraplength=760).pack(anchor="w", pady=6)
        table = ttk.Frame(page)
        table.pack(fill="x", pady=6)
        self.geometry_tree = ttk.Treeview(table, columns=("material", "z", "pieces", "status"), show="tree headings", height=5, selectmode="browse")
        for key, title in (("#0", "工艺步骤 / 图例"), ("material", "材料"), ("z", "最终 z 范围 · µm"), ("pieces", "几何分片"), ("status", "状态")):
            self.geometry_tree.heading(key, text=title)
            self.geometry_tree.column(key, width=205 if key == "#0" else 155, minwidth=80)
        self.geometry_tree.column("pieces", width=75, minwidth=65, stretch=False)
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.geometry_tree.yview)
        self.geometry_tree.configure(yscrollcommand=scrollbar.set)
        self.geometry_tree.pack(side="left", fill="x", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.geometry_groups = {}
        self.geometry_color_icons = []
        ttk.Label(page, text="每行对应一个工艺步骤，色块与图中图例一致；展开可查看计算分片。分片不是新增膜层。", style="Muted.TLabel", wraplength=800).pack(anchor="w", pady=(0, 5))
        self.geometry_image = ImageView(page)
        # Keep section labels and the shared legend readable in the main page.
        # ScrollFrame supplies vertical scrolling; fill=x follows the viewport.
        self.geometry_image.configure(height=680)
        self.geometry_image.pack(fill="both", expand=True)

    def show_geometry(self, folder, geometry):
        self.geometry_folder = Path(folder); self.geometry_tree.delete(*self.geometry_tree.get_children())
        self.geometry_groups = {}
        self.geometry_color_icons = []
        if geometry.get("representation") == "voxel_labels":
            self.geometry_preview_folder = Path(folder)
            self.geometry_description.set("导入体素结构；可预览，当前 periodic_2d 求解器不支持，禁止自动降维。材料与坐标保持原样。")
            self.clear_process_geometry()
            self.geometry_image.clear()
            image = Path(folder)/"voxel_sections.png"
            if image.is_file(): self.geometry_image.show(image)
            for index, name in geometry.get("material_map", {}).items():
                self.geometry_tree.insert("", "end", text="标签 "+str(index), values=(name,"体素坐标","—","只读"))
            return
        groups = step_groups(geometry)
        self.geometry_tree.configure(height=max(3, min(6, len(groups))))
        for i, group in enumerate(groups):
            row_id = f"group_{i}"
            self.geometry_groups[row_id] = group
            icon = tk.PhotoImage(master=self, width=14, height=14)
            icon.put(group["color"], to=(1, 1, 13, 13))
            self.geometry_color_icons.append(icon)
            interval = "—" if group["z_min_um"] is None else f"{group['z_min_um']:g} … {group['z_max_um']:g}"
            self.geometry_tree.insert("", "end", iid=row_id, text=group["label"], image=icon, open=False,
                                      values=(group["material"] or "—", interval, group["count"], group["status"]))
            for j, solid in enumerate(group["solids"]):
                p = np.asarray(solid["polygon_xy_um"])
                extent = np.ptp(p, axis=0)
                self.geometry_tree.insert(row_id, "end", iid=f"{row_id}_part_{j}", text=f"分片 {j + 1} · {solid.get('name', '')}",
                    values=(solid["material"], f"{solid['z_min_um']:g} … {solid['z_max_um']:g}", "1",
                            f"x/y: {extent[0]:g} / {extent[1]:g} µm"))
        steps = sum(group["source_known"] for group in groups)
        description = f"母周期 {geometry.get('period_x_um')} µm；{steps} 个工艺步骤，{len(geometry.get('solids', []))} 个计算分片。"
        from ...result_inspection import geometry_optical_warnings
        warnings = list(dict.fromkeys([
            *geometry.get("warnings", []), *geometry_optical_warnings(geometry)]))
        if warnings:
            description += "\n" + "\n".join(warnings)
            for warning in warnings:
                self.append_log("几何提示：" + str(warning))
        self.geometry_description.set(description + f"\n原始几何目录：{folder}")

        # A historical PNG used material colors. Never pair it with the new
        # step-color table or overwrite it: render a separate read-only view.
        from ...geometry_preview import preview_geometry, preview_matches
        if not hasattr(self, "_geometry_view_cache"):
            self._geometry_view_cache = {}
        cached = self._geometry_view_cache.get(str(self.geometry_folder.resolve()))
        if preview_matches(geometry, folder):
            preview_folder = Path(folder)
        elif cached is not None and preview_matches(geometry, cached):
            preview_folder = cached
        else:
            from ..paths import ROOT
            view_root = self.data["folder"] / "geometry" / "views" if self.data else ROOT / "outputs" / "geometry_views"
            preview_folder = service.unique_folder(view_root, "view")
            preview_geometry(geometry, preview_folder)
            self._geometry_view_cache[str(self.geometry_folder.resolve())] = preview_folder
        self.geometry_preview_folder = preview_folder
        image = preview_folder / "geometry_sections.png"
        if image.exists(): self.geometry_image.show(image)
        self.update_process_geometry(preview_folder, geometry)

    def edit_geometry_step(self):
        """Jump by stable step ID; current recipe order can differ from a snapshot."""
        self._assert_recipe_editable()
        selection = self.geometry_tree.selection()
        if not selection:
            raise ValueError("先选中几何表中的工艺步骤")
        row_id = selection[0]
        parent = self.geometry_tree.parent(row_id)
        group = self.geometry_groups.get(parent or row_id)
        if not group or not group["source_known"] or group["id"] is None:
            raise ValueError("此历史几何没有可靠的工艺步骤来源")
        if not self.data:
            raise ValueError("先打开对应工程")
        matches = [index for index, step in enumerate(self.data["process"]["steps"]) if step.get("id") == group["id"]]
        if len(matches) != 1:
            raise ValueError("当前工程中已无此步骤；请查看原始几何快照")
        self.tabs.select(1)
        self.process_tree.selection_set(str(matches[0]))
        self.process_tree.see(str(matches[0]))
        self.select_step()

    def open_geometry_3d(self):
        if not self.geometry_folder: raise ValueError("先生成几何")
        path = getattr(self, "geometry_preview_folder", self.geometry_folder)/"geometry_3d.html"
        if not path.exists(): path = getattr(self, "geometry_preview_folder", self.geometry_folder)/"voxel_3d.html"
        if not path.exists(): raise ValueError("当前几何尚无 3D 预览")
        webbrowser.open(path.resolve().as_uri())

    def open_geometry_sections(self):
        if not self.geometry_folder:
            raise ValueError("先生成几何")
        path = getattr(self, "geometry_preview_folder", self.geometry_folder) / "geometry_sections.png"
        if not path.is_file(): path = getattr(self, "geometry_preview_folder", self.geometry_folder) / "voxel_sections.png"
        if not path.is_file():
            raise ValueError("当前几何尚无截面图")
        webbrowser.open(path.resolve().as_uri())

    def choose_geometry_snapshot(self):
        if self.data is None: raise ValueError("请先打开工程，以保存几何快照预览")
        path = filedialog.askopenfilename(initialdir=self.geometry_folder, filetypes=[("Geometry JSON", "*.json")])
        if not path: return
        from ...geometry import preview_geometry
        value = read_json(path); output = service.unique_folder(self.data["folder"]/"geometry", "snapshot_view")
        preview_geometry(value, output); write_json(output/"geometry.json", value); self.show_geometry(output, value)
