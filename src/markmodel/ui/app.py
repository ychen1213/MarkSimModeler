"""Five-page editor. Long operations run off the Tk thread; all edits stay local."""
from __future__ import annotations

import copy
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from uuid import uuid4
import webbrowser

from .widgets import ImageView, JsonDialog, ScrollPage, card, hint, style, configure_dpi
from .process import ProcessPage
from .materials import MaterialsPage
from .transfer import TransferPages

ROOT = Path(__file__).resolve().parents[3]


class ModelerApp(ProcessPage, MaterialsPage, TransferPages, ttk.Frame):
    """UI owns drafts only. service owns geometry, files, and validation."""
    def __init__(self, master, project=None):
        super().__init__(master, padding=16)
        style(master)
        self.pack(fill="both", expand=True)
        self.data = None
        self.model = None
        self.model_origin = None
        self.preview_dir = None
        self.dirty = False
        self.busy = False
        self._input_revision = 0
        self._buttons = []
        self._automatic_disabled = {}
        self._busy_widgets = []
        self._task_started = None
        self.last_error_path = None
        self._busy_tag=f'ModelerBusy{id(self)}'
        for sequence in ('<ButtonPress>','<KeyPress>','<<TreeviewSelect>>'):
            self.bind_class(self._busy_tag,sequence,lambda event:'break' if self.busy else None)
        self._messages = queue.Queue()
        self._poll_id = None
        self.status = tk.StringVar(value="新建或打开建模工程。建模工具不启动任何 FDTD 求解器。")
        self.project_label = tk.StringVar(value="未打开工程")
        header = ttk.Frame(self)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="MarkSim  /  三维结构建模", style="Title.TLabel").pack(side="left")
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 8))
        for text, command in (("新建…", self.new_dialog), ("打开工程…", self.open_dialog),
                              ("保存输入", self.save_async), ("生成 / 更新结构", self.build_async)):
            self.button(toolbar, text, command, primary="生成" in text)
        project_line=ttk.Label(self,textvariable=self.project_label,style="Hint.TLabel",wraplength=1100)
        project_line.pack(anchor="w",fill='x',pady=(0,8))
        self.next_step=tk.StringVar(value='从这里开始：新建工程，或到「图像与体素」加载 SEM/TEM 或已有结构。')
        ttk.Label(self,textvariable=self.next_step,style='Hint.TLabel',wraplength=1100).pack(anchor='w',fill='x',pady=(0,8))
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True)
        self.pages = []
        for label in ("01 工程与材料", "02 版图与工艺", "03 几何预览", "04 图像与体素", "05 导出与报告"):
            page = ScrollPage(self.tabs)
            self.tabs.add(page, text=label)
            self.pages.append(page.body)
        self.create_materials_page(self.pages[0])
        self.create_process_page(self.pages[1])
        self.create_geometry_page(self.pages[2])
        self.create_image_page(self.pages[3])
        self.create_export_page(self.pages[4])
        self.progress = ttk.Progressbar(self, mode="determinate",value=0)
        self.progress.pack(fill="x", pady=(10, 4))
        self.status_label = ttk.Label(self, textvariable=self.status, wraplength=1200, style="Hint.TLabel")
        self.status_label.pack(fill="x")
        footer=ttk.Frame(self);footer.pack(fill='x')
        self.task_note=tk.StringVar(value='就绪')
        ttk.Label(footer,textvariable=self.task_note,style='Hint.TLabel').pack(side='left')
        self.error_button=ttk.Button(footer,text='查看最近错误详情',command=lambda:self.open_path(self.last_error_path),state='disabled')
        self.error_button.pack(side='right')
        # Reserve the bottom feedback before allocating the stretchable page;
        # packing it after the notebook hid it entirely on 950x680 displays.
        self.tabs.pack_forget();self.progress.pack_forget();self.status_label.pack_forget();footer.pack_forget()
        footer.pack(side='bottom',fill='x')
        self.status_label.pack(side='bottom',fill='x')
        self.progress.pack(side='bottom',fill='x',pady=(6,4))
        self.tabs.pack(fill='both',expand=True)
        self.bind('<Configure>',lambda e: (project_line.configure(wraplength=max(300,e.width-40)),self.status_label.configure(wraplength=max(300,e.width-40))) if e.widget is self else None,add='+')
        self.bind("<Destroy>", self._destroy, add="+")
        self._poll_id = self.after(100, self._poll)
        if project:
            self.open_project(project)
        self._update_buttons()
        master.bind('<Control-s>',lambda event:self.safe(self.save_async))

    def button(self, parent, label, command, *, primary=False):
        button = ttk.Button(parent, text=label, command=lambda: self.safe(command),
                            style="Primary.TButton" if primary else "TButton")
        button.pack(side="left", padx=(0, 8), pady=4)
        self._buttons.append((button,getattr(command,'__name__','')))
        return button

    def _update_buttons(self):
        live=[]
        for button,name in self._buttons:
            if button.winfo_exists():live.append((button,name))
            else:self._automatic_disabled.pop(button,None)
        self._buttons=live
        project_actions={'save','save_async','build_async','apply_mark','add_step','edit_recipe',
                         'material_import_dialog','constant_material_dialog','edit_material'}
        model_actions={'export_dialog','preview_export_async','voxelize_async'}
        for button,name in self._buttons:
            disabled=(self.busy or (name in project_actions and not self.data)
                      or (name in model_actions and self.model is None)
                      or (name=='open_3d' and self.preview_dir is None))
            if disabled:
                if button not in self._automatic_disabled:
                    self._automatic_disabled[button]=button.instate(['disabled'])
                button.state(['disabled'])
            elif button in self._automatic_disabled:
                originally_disabled=self._automatic_disabled.pop(button)
                if not originally_disabled:button.state(['!disabled'])

    def _set_busy(self,busy):
        self.busy=busy
        if busy:
            self._busy_widgets=[]
            pending=[self.tabs]
            while pending:
                widget=pending.pop();pending.extend(widget.winfo_children())
                if isinstance(widget,(ttk.Entry,ttk.Combobox,ttk.Checkbutton,ttk.Radiobutton,ttk.Treeview)):
                    if self._busy_tag not in widget.bindtags():
                        widget.bindtags((self._busy_tag,*widget.bindtags()))
                    self._busy_widgets.append((widget,widget.state()))
                    widget.state(['disabled'])
            self._task_started=time.monotonic()
        else:
            for widget,state in self._busy_widgets:
                if widget.winfo_exists():
                    widget.state(['!disabled','!readonly']);widget.state(list(state))
            self._busy_widgets=[];self._task_started=None
            self.task_note.set('就绪')
            if hasattr(self,'refresh_export_controls'):self.refresh_export_controls()
            if hasattr(self,'_update_step_controls'):self._update_step_controls()
        self._update_buttons()

    def report_error(self,exc,title='操作未完成'):
        folder=ROOT/'logs';folder.mkdir(exist_ok=True)
        path=folder/('ui_error_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid4().hex[:6]+'.log')
        detail=''.join(traceback.format_exception(type(exc),exc,exc.__traceback__))
        path.write_text(detail,encoding='utf-8')
        self.last_error_path=path;self.error_button.configure(state='normal')
        text=str(exc) or type(exc).__name__
        self.status.set(title+'：'+text)
        messagebox.showerror(title,text+'\n\n详细记录已保存，可点击窗口底部“查看最近错误详情”。',parent=self.winfo_toplevel())

    def safe(self, command):
        try:
            if self.busy:
                raise ValueError("当前任务尚未结束，请等待状态栏完成后再操作。")
            return command()
        except Exception as exc:
            self.report_error(exc,'请检查输入')

    def _destroy(self, event):
        if event.widget is self and self._poll_id:
            self.after_cancel(self._poll_id)
            self._poll_id = None
            for sequence in ('<ButtonPress>','<KeyPress>','<<TreeviewSelect>>'):
                self.unbind_class(self._busy_tag,sequence)

    def _poll(self):
        try:
            while True:
                ok, value, done = self._messages.get_nowait()
                self._set_busy(False)
                self.progress.stop()
                self.progress.configure(mode='determinate',value=0)
                if ok:
                    try:
                        done(value)
                    except Exception as exc:
                        self.report_error(exc,'结果显示失败')
                else:
                    self.report_error(value,'任务未完成')
        except queue.Empty:
            pass
        if self.busy and self._task_started is not None:
            seconds=int(time.monotonic()-self._task_started)
            self.task_note.set(f'正在处理 · 已用 {seconds} 秒 · 完成前暂不能修改输入')
        self._update_buttons()
        self._poll_id = self.after(100, self._poll)

    def work(self, label, task, done):
        if self.busy:
            raise ValueError("已有运行中的建模任务")
        self._set_busy(True)
        self.status.set(label)
        self.progress.configure(mode='indeterminate')
        self.progress.start(15)
        revision = self._input_revision
        def accepted(value):
            if (isinstance(value,dict) and value.get('source_image_revision') is not None
                    and value['source_image_revision']!=self._image_revision):
                self.status.set('结果已保存，但来源图像配置已改变；请重新重建或转换。')
                return
            if (isinstance(value, dict) and value.get("origin") == "image"
                    and value.get("image_revision") != self._image_revision):
                self.status.set("图像重建快照已保存，但标定/配置在运行中已改变；请重新重建。")
                return
            if isinstance(value, dict) and "model" in value and self._input_revision != revision:
                self.model = None
                self.status.set("任务快照已保存，但运行期间输入已改变；请重新生成，旧快照不会作为当前导出结构。")
                return
            done(value)
        def execute():
            try:
                self._messages.put((True, task(), accepted))
            except Exception as exc:
                self._messages.put((False, exc, done))
        threading.Thread(target=execute, daemon=True, name="modeler-build").start()

    def require_project(self):
        if not self.data:
            raise ValueError("请先新建或打开工程")
        return self.data

    def changed(self):
        self._input_revision += 1
        self.dirty = True
        self.model = None
        self.geometry_note.set("输入已改变；之前的图片仅供参照。重新生成后才可导出。")
        if self.data:self.project_label.set(str(self.data["folder"]) + "  • 输入未保存")
        self.next_step.set('输入已修改：保存或生成结构，然后核对预览再导出。')
        self._update_buttons()

    def output_folder(self, label):
        base = Path(self.data["folder"]) if self.data else ROOT
        return base / "outputs" / (label + "_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6])

    def confirm_replace(self,action='切换工程'):
        if self.dirty or getattr(self, "step_dirty", False) or getattr(self,'mark_dirty',False):
            answer=messagebox.askyesnocancel('尚未保存',f'当前工程有未保存修改。是否先保存再{action}？\n\n是：保存后继续　否：放弃修改　取消：留在当前工程',parent=self.winfo_toplevel())
            if answer is None:return False
            if answer:self.save()
        return True

    def new_dialog(self):
        if not self.confirm_replace():
            return
        folder = filedialog.asksaveasfilename(title="新工程文件夹名称（必须尚不存在）", initialfile="AH53_Model", parent=self.winfo_toplevel())
        if folder:
            self.new_project(folder, self.mark_vars["mark_type"].get())

    def new_project(self, folder, mark_type="AH53"):
        from .. import service
        service.create_project(folder, mark_type=mark_type)
        self.open_project(folder)
        return self.data

    def open_dialog(self):
        if not self.confirm_replace():
            return
        folder = filedialog.askdirectory(title="选择包含 project.yaml 的工程目录", parent=self.winfo_toplevel())
        if folder:
            self.open_project(folder)

    def open_project(self, folder):
        from .. import service
        self.data = service.load_project(folder)
        self._input_revision += 1
        self.model = None
        self.model_origin = None
        self.preview_dir = None
        self.dirty = False
        self.mark_dirty=False
        self.step_dirty=False
        self.geometry_image.clear()
        self.geometry_tree.delete(*self.geometry_tree.get_children())
        self.last_export=None
        self.material_mapping={}
        self.export_report.configure(state='normal');self.export_report.delete('1.0','end');self.export_report.configure(state='disabled')
        self.project_label.set(str(self.data["folder"]))
        self.refresh_materials()
        self.refresh_steps()
        self.refresh_mark()
        self.geometry_note.set("已打开输入。点击生成结构以得到与当前输入绑定的几何。")
        self.status.set("工程已打开；材料与工艺均来自该工程的本地文件。")
        self.next_step.set('下一步：检查材料与工艺 → 生成结构 → 核对预览 → 导出。')
        self._update_buttons()

    def _commit_forms(self):
        if hasattr(self,'commit_pending_inputs'):
            self.commit_pending_inputs()
        elif self.step_dirty:self.apply_step()

    def _mark_saved(self,result):
        self.dirty=False
        self.project_label.set(str(self.data['folder']))
        self.status.set('输入已保存；可以生成结构。历史版本保留在工程目录。')
        return result

    def save(self):
        from .. import service
        self.require_project()
        self._commit_forms()
        result = service.save_project(self.data["folder"], self.data["process"], self.data["materials"])
        return self._mark_saved(result)

    def save_async(self):
        from .. import service
        self.require_project();self._commit_forms()
        folder=self.data['folder'];process=copy.deepcopy(self.data['process']);materials=copy.deepcopy(self.data['materials'])
        revision=self._input_revision
        def done(result):
            if revision==self._input_revision:self._mark_saved(result)
            else:self.status.set('操作开始时的输入已保存；之后的新修改仍未保存。')
        self.work('正在校验并保存输入…',lambda:service.save_project(folder,process,materials),done)

    def build_now(self, previews=True):
        """Synchronous entry for script/debug tests; toolbar uses the worker."""
        from .. import service
        self.save()
        result = service.build_project(self.data["folder"], previews=previews)
        self.accept_build(result)
        return result

    def build_async(self):
        from .. import service
        self.require_project();self._commit_forms()
        folder=Path(self.data['folder']);process=copy.deepcopy(self.data['process']);materials=copy.deepcopy(self.data['materials'])
        def task():
            service.save_project(folder,process,materials)
            return service.build_project(folder,process=process,materials=materials)
        def done(result):
            self._mark_saved(result);self.accept_build(result)
        self.work('正在校验输入、保存并按工艺顺序生成几何…',task,done)

    def accept_build(self, result):
        self.model = result["model"]
        self.model_origin = result.get("origin", "process")
        self.material_mapping={}
        self.preview_dir = Path(result["output_dir"])
        self.show_model(result.get("previews"))
        self.status.set("结构生成完成：" + str(self.preview_dir))
        self.tabs.select(2)
        self.next_step.set('结构已生成：核对截面与材料图例；确认后到「导出与报告」。')
        self._update_buttons()

    def create_geometry_page(self, page):
        box = card(page, "当前结构 · 图例与工艺步骤一一对应")
        self.geometry_note = tk.StringVar(value="尚未生成结构")
        ttk.Label(box, textvariable=self.geometry_note, style="Hint.TLabel", wraplength=1100).pack(fill="x")
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.button(row, "打开交互 3D", self.open_3d)
        self.button(row, '放大截面图', self.open_section)
        self.button(row, "打开输出目录", lambda: self.open_path(self.preview_dir))
        self.geometry_image = ImageView(box)
        self.geometry_image.pack(fill="both", expand=True, pady=8)
        self.geometry_image.label.bind('<Double-Button-1>',lambda event:self.safe(self.open_section))
        self.geometry_tree = ttk.Treeview(box, columns=("material", "z", "status"), show="tree headings", height=7)
        for key, title, width in (("#0", "工艺步骤 / 对象", 300), ("material", "材料", 220), ("z", "z 范围 µm", 180), ("status", "状态", 160)):
            self.geometry_tree.heading(key, text=title)
            self.geometry_tree.column(key, width=width, minwidth=80)
        self.geometry_tree.pack(fill="x")
        hint(box, "一个步骤可能拆分成多个计算实体；表中按原步骤合并。体素结构显示材料标签，不虚构工艺来源。")

    def show_model(self, previews=None):
        from marksim.geometry_display import step_groups
        geom = self.model["geometry"]
        self.geometry_tree.delete(*self.geometry_tree.get_children())
        for i, group in enumerate(step_groups(geom)):
            interval = "—" if group["z_min_um"] is None else f"{group['z_min_um']:g} → {group['z_max_um']:g}"
            self.geometry_tree.insert("", "end", text=group["label"], values=(group["material"], interval, group["status"]), tags=(str(i),))
            self.geometry_tree.tag_configure(str(i), foreground=group["color"])
        if self.model.get("voxels") is not None:
            for label, material in self.model["voxels"]["material_map"].items():
                self.geometry_tree.insert("", "end", text=f"体素 material_id={label}", values=(material, "见坐标范围", "来源：标签体素"))
        candidates = list(self.preview_dir.glob("*.png")) if self.preview_dir else []
        candidates.sort(key=lambda p: ("sections" not in p.name, p.name))
        if candidates:
            self.geometry_image.show(candidates[0])
        else:self.geometry_image.clear('此结构没有已生成的截面图。')
        origin = {"process": "当前工程工艺", "image": "SEM/TEM 重建", "import": "外部几何快照，未替换当前工程工艺", "voxel": "显式体素转换"}.get(self.model_origin, "几何")
        representation={'parametric_solids':'参数化多材料实体','voxel_labels':'三维材料体素'}.get(geom.get('representation'),'几何')
        self.geometry_note.set(f"{origin} · {representation} · 单位 µm · 当前快照可导出")

    def open_section(self):
        if not self.geometry_image.path:raise ValueError('先生成或加载结构截面图。')
        self.open_path(self.geometry_image.path)

    def open_3d(self):
        if not self.preview_dir:
            raise ValueError("先生成或加载结构预览")
        files = list(self.preview_dir.glob("*.html"))
        if not files:
            raise ValueError("此结果没有交互 3D 文件；请使用截面预览或导出 STL。")
        webbrowser.open(files[0].as_uri())

    @staticmethod
    def open_path(path):
        if path is None or not Path(path).exists():
            raise ValueError("输出路径尚不存在")
        if os.name == "nt":
            os.startfile(str(Path(path).resolve()))
        else:
            webbrowser.open(Path(path).resolve().as_uri())


def launch(project=None):
    configure_dpi()
    root = tk.Tk()
    root.title("MarkSim Structure Modeler · 独立源码版")
    root.geometry("1320x900")
    root.minsize(950, 680)
    app = ModelerApp(root, project=project)
    def close():
        if app.busy:
            messagebox.showinfo("任务运行中", "请等待当前导出或重建完成再关闭。", parent=root)
            return
        try:
            if app.confirm_replace(action='关闭窗口'):root.destroy()
        except Exception as exc:app.report_error(exc,'保存失败，窗口保持打开')
    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()
