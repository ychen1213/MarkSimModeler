"""Read-only raw material imports and n/k inspection."""
from __future__ import annotations
from pathlib import Path
import shutil
from tkinter import filedialog


class MaterialsSection:
    """Feature methods sharing the single MarkSimApp state owner."""

    def refresh_materials(self):
        self.material_tree.delete(*self.material_tree.get_children())
        if not self.data: return
        self.step_material_combo.configure(values=list(self.data["materials"]["materials"]))
        for name, item in self.data["materials"]["materials"].items():
            label = "已提供 k" if item.get("k") is not None else "k 未知 / 明确无损近似" if item.get("assume_lossless") else "k 未知 / 禁止仿真"
            self.material_tree.insert("", "end", iid=name, text=name, values=(str(item.get("valid_range_nm")), label, item.get("source")))

    def import_materials(self):
        self._assert_recipe_editable()
        if not self.data: raise ValueError("先打开工程")
        folder = filedialog.askdirectory(title="选择只读材料 TXT 来源文件夹")
        if not folder: return
        from ...materials import import_material_txt
        columns = [int(x.strip())-1 for x in self.material_columns.get().split(",")]
        if len(columns) not in (2, 3): raise ValueError("填写 λ,n 或 λ,n,k 的列号")
        mapping = dict(zip(("wavelength", "n", "k"), columns))
        paths = sorted(Path(folder).glob("*.txt"))
        if not paths: raise ValueError("文件夹中没有 .txt 文件")
        imported = {p.stem: import_material_txt(p, wavelength_unit=self.material_unit.get(), columns=mapping, assume_lossless=self.assume_lossless.get()) for p in paths}
        for name, item in imported.items():
            destination = self.data["folder"]/"materials"/(name+"_"+item["source_sha256"][:10]+".txt")
            if not destination.exists(): destination.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(item["source"], destination)
            item["source"] = str(destination)
        self.data["materials"]["materials"].update(imported)
        self.refresh_materials(); self.mark_changed("physics")

    def plot_material(self):
        selection = self.material_tree.selection()
        if not selection: return
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        item = self.data["materials"]["materials"][selection[0]]
        if self.material_canvas: self.material_canvas.get_tk_widget().destroy()
        fig = Figure(figsize=(8, 2.6), dpi=95); axes = fig.subplots(1, 2)
        axes[0].plot(item["wavelength_nm"], item["n"], "o-"); axes[0].set(xlabel="Vacuum wavelength (nm)", ylabel="n")
        if item.get("k") is not None: axes[1].plot(item["wavelength_nm"], item["k"], "o-")
        else: axes[1].text(.5, .5, "k unknown"+("; explicit lossless approximation" if item.get("assume_lossless") else ""), ha="center", transform=axes[1].transAxes)
        axes[1].set(xlabel="Vacuum wavelength (nm)", ylabel="k"); fig.tight_layout()
        self.material_canvas = FigureCanvasTkAgg(fig, self.material_plot); self.material_canvas.draw(); self.material_canvas.get_tk_widget().pack(fill="both", expand=True)
