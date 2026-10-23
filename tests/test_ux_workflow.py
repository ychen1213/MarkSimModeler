"""User workflow regressions: save/discard, stale views and real background work."""
import copy
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import ttk

import pytest
from markmodel import service
from markmodel.ui import ModelerApp
from markmodel.ui.widgets import ScrollPage,JsonDialog


@pytest.fixture(scope='module')
def tk_root():
    root=tk.Tk();root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(tmp_path,tk_root):
    window=tk.Toplevel(tk_root);window.withdraw()
    editor=ModelerApp(window)
    editor.new_project(tmp_path/'project')
    yield editor
    window.update_idletasks()
    window.destroy()


def test_switch_project_clears_previous_geometry_report_and_mapping(app,tmp_path):
    app.build_now(previews=True)
    app.export_now(tmp_path/'export')
    app.material_mapping={'RIDGE_DEMO':'old material'}
    assert app.geometry_image.path is not None
    app.new_project(tmp_path/'second','AH32')
    assert app.model is None and app.preview_dir is None
    assert app.geometry_image.path is None and not app.geometry_tree.get_children()
    assert app.last_export is None and not app.material_mapping
    assert not app.export_report.get('1.0','end').strip()


def test_open_invalid_project_keeps_current_project_and_draft(app,tmp_path):
    app.mark_vars['cd_nm'].set('1200')
    before=app.data
    with pytest.raises(ValueError,match='project.yaml'):app.open_project(tmp_path)
    assert app.data is before and app.mark_dirty
    assert app.mark_vars['cd_nm'].get()=='1200'


def test_close_or_switch_has_save_discard_cancel_and_save_failure_protection(app,monkeypatch):
    from markmodel.ui import app as app_module
    app.mark_vars['cd_nm'].set('1200')
    monkeypatch.setattr(app_module.messagebox,'askyesnocancel',lambda *a,**k:None)
    assert app.confirm_replace() is False and app.mark_dirty
    monkeypatch.setattr(app_module.messagebox,'askyesnocancel',lambda *a,**k:True)
    assert app.confirm_replace(action='关闭窗口') is True
    assert not app.mark_dirty and not app.dirty
    assert service.load_project(app.data['folder'])['process']['mark']['cd_um']==pytest.approx(1.2)
    app.mark_vars['cd_nm'].set('not a number')
    with pytest.raises(ValueError):app.confirm_replace()
    assert app.mark_dirty


def test_async_build_keeps_event_loop_alive_and_restores_controls(app,monkeypatch):
    original=service.save_project;entered=threading.Event();release=threading.Event()
    def gated(*args,**kwargs):
        entered.set()
        if not release.wait(10):raise RuntimeError('test gate expired')
        return original(*args,**kwargs)
    monkeypatch.setattr(service,'save_project',gated)
    app.build_async()
    assert entered.wait(2) and app.busy
    assert app.mark_combos['cd_nm'].instate(['disabled'])
    ping=[];app.after(1,lambda:ping.append(True))
    until=time.monotonic()+2
    while not ping and time.monotonic()<until:app.update();time.sleep(.01)
    assert ping
    release.set();until=time.monotonic()+30
    while app.busy and time.monotonic()<until:app.update();time.sleep(.01)
    assert not app.busy and app.model is not None
    assert not app.mark_combos['cd_nm'].instate(['disabled'])


def test_save_rejects_undefined_material_before_touching_disk_and_avoids_empty_revisions(app):
    folder=app.data['folder'];before=(folder/'process.yaml').read_bytes()
    recipe=copy.deepcopy(app.data['process']);recipe['steps'][1]['material']='MISSING'
    with pytest.raises(ValueError,match='Missing optical'):service.save_project(folder,recipe,app.data['materials'])
    assert (folder/'process.yaml').read_bytes()==before
    report=service.save_project(folder,app.data['process'],app.data['materials'])
    assert report['unchanged'] is True and report['revision_dir'] is None


def test_wheel_over_child_label_scrolls_only_its_page_and_keeps_text_scroll(tk_root):
    root=tk.Toplevel(tk_root);root.geometry('450x260')
    try:
        page=ScrollPage(root);page.pack(fill='both',expand=True)
        labels=[ttk.Label(page.body,text=f'row {i}') for i in range(40)]
        for label in labels:label.pack()
        editor=tk.Text(page.body,height=3);editor.pack()
        root.update()
        start=page.canvas.yview()
        labels[0].event_generate('<MouseWheel>',delta=-120);root.update()
        assert page.canvas.yview()[0]>start[0]
        before=page.canvas.yview()
        editor.event_generate('<MouseWheel>',delta=-120);root.update()
        assert page.canvas.yview()==before
    finally:root.destroy()


def test_modal_json_editor_keeps_project_context(app):
    dialog=JsonDialog(app,'test editor',{'key':'value'},lambda x:None)
    try:
        assert dialog.grab_current() is dialog
        assert str(dialog.transient())==str(app.winfo_toplevel())
        assert dialog.text.cget('yscrollcommand')
    finally:dialog.destroy()


def test_busy_tree_selection_cannot_reenable_step_fields(app):
    app.step_tree.selection_set('0');app.select_step();app.update()
    app._set_busy(True)
    try:
        app.step_tree.focus('0')
        app.step_tree.tk.call('ttk::treeview::Keynav',str(app.step_tree),'down')
        app.update()
        assert app.step_index==0
        assert app.step_controls['thickness'].instate(['disabled'])
    finally:app._set_busy(False)


def test_closed_gds_dialog_does_not_break_next_task(app,monkeypatch):
    from markmodel.ui import process as page
    app.step_tree.selection_set('1');app.select_step()
    monkeypatch.setattr(page.filedialog,'askopenfilename',lambda **kwargs:str(app.data['folder']/'inputs/AH53.gds'))
    app.gds_dialog()
    dialogs=[widget for widget in app.winfo_children() if isinstance(widget,tk.Toplevel)]
    assert dialogs
    for dialog in dialogs:dialog.destroy()
    app._set_busy(True)
    assert app.busy
    app._set_busy(False)
    assert not app.busy and all(button.winfo_exists() for button,name in app._buttons)


def test_small_window_keeps_status_footer_and_all_tabs_visible(app):
    root=app.winfo_toplevel();root.deiconify();root.geometry('950x680');root.update()
    assert app.status_label.winfo_y()+app.status_label.winfo_height()<=app.winfo_height()
    assert app.error_button.winfo_rooty()+app.error_button.winfo_height()<=root.winfo_rooty()+root.winfo_height()
    # Last tab must remain inside the notebook instead of truncating its name.
    visible={str(app.tabs.tk.call(str(app.tabs),'identify','tab',x,16)) for x in range(app.tabs.winfo_width())}
    assert '4' in visible


def test_unopened_output_path_has_no_duplicate_outputs(tmp_path,tk_root):
    root=tk.Toplevel(tk_root);root.withdraw()
    try:
        app=ModelerApp(root)
        target=app.output_folder('demo')
        assert target.parent.name=='outputs' and target.parent.parent.name!='outputs'
        root.update_idletasks()  # Hidden-window layout must reach a stable size.
    finally:root.destroy()
