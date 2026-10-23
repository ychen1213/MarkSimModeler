"""Diagnostic only: create the real application, process events, then close it."""
from pathlib import Path
import sys
import tkinter as tk

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from markmodel.ui import ModelerApp
from markmodel.ui.widgets import configure_dpi


def main():
    configure_dpi()
    root=tk.Tk()
    root.title('MarkSimModeler - startup diagnostic')
    root.geometry('1180x800')
    failures=[]
    root.report_callback_exception=lambda kind,value,tb:failures.append(str(value))
    app=ModelerApp(root)
    root.update()
    assert root.winfo_exists() and len(app.tabs.tabs())==5
    root.after(750,root.destroy)
    root.mainloop()
    if failures:raise RuntimeError('; '.join(failures))
    print('GUI_SMOKE_OK: real Tk window and all five pages initialized; event loop exited normally.')


if __name__=='__main__':main()
