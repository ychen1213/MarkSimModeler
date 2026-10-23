"""Capture only the modeler's client area after real build/export, for local QA."""
from __future__ import annotations
import json
from pathlib import Path
import sys
import time
import tkinter as tk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from markmodel.ui import ModelerApp
from markmodel.service import unique_folder
from markmodel.ui.widgets import configure_dpi


def main():
    from PIL import ImageGrab
    output = unique_folder(ROOT / "outputs", "gui_review")
    configure_dpi()
    root = tk.Tk()
    root.title("MarkSimModeler · Real GUI review")
    root.geometry("1280x900+80+60")
    app = ModelerApp(root)
    app.new_project(output / "AH53_GUI_DEMO")
    app.add_step()
    app.step_vars["thickness"].set("100")
    app.step_vars["sidewall"].set("50")
    app.apply_step()
    app.build_now(previews=True)
    app.export_now(output / "export")
    paths = {}
    for index, name in ((0, "materials"), (1, "process"), (2, "geometry"), (4, "export")):
        app.tabs.select(index)
        if index == 0:
            app.material_tree.selection_set("RIDGE_DEMO")
            app.select_material()
        root.update()
        # Tk rendering must complete before screen capture; no hidden desktop.
        time.sleep(.2)
        root.update_idletasks()
        bounds = (root.winfo_rootx(), root.winfo_rooty(), root.winfo_rootx() + root.winfo_width(), root.winfo_rooty() + root.winfo_height())
        path = output / (name + ".png")
        ImageGrab.grab(bbox=bounds).save(path)
        paths[name] = str(path)
    root.destroy()
    (output / "review.json").write_text(json.dumps({"kind": "real_gui_build_and_export", "screenshots": paths}, indent=2), encoding="utf-8")
    print(json.dumps({"directory": str(output), "screenshots": paths}, ensure_ascii=False))


if __name__ == "__main__":
    main()
