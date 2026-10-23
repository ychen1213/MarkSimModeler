"""Capture only our own new QA window; never touch an existing user session."""
from pathlib import Path
import ctypes
import json
import sys
import tkinter as tk
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from markmodel.ui import ModelerApp
from markmodel.ui.widgets import configure_dpi
from markmodel.service import unique_folder


def main():
    from PIL import ImageGrab
    output=unique_folder(ROOT/'outputs/ux_audit','visual')
    configure_dpi()
    root=tk.Tk();root.geometry('1280x900+50+50');root.title('MarkSimModeler - UX review')
    app=ModelerApp(root);app.new_project(output/'AH53');app.add_step()
    app.step_vars['thickness'].set('100');app.step_vars['sidewall'].set('50')
    app.build_now(previews=True);app.export_now(output/'export')
    captures=[]
    for size,pages in [('1280x900',range(5)),('950x680',[1,4])]:
        root.geometry(size+'+50+50')
        for page in pages:
            app.tabs.select(page);root.update();time.sleep(.1);root.update_idletasks()
            hwnd=ctypes.windll.user32.GetAncestor(root.winfo_id(),2)
            path=output/f'{size}_page_{page+1}.png'
            ImageGrab.grab(window=hwnd).save(path)
            captures.append(str(path))
    root.destroy()
    (output/'review.json').write_text(json.dumps({'captures':captures,'real_build_and_export':True},ensure_ascii=False,indent=2),encoding='utf-8')
    print(output)


if __name__=='__main__':main()
