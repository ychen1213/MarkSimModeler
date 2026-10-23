"""Small reusable Tk controls; no geometry or solver calculations here."""
from __future__ import annotations

import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk

BG = "#f2f5fa"
INK = "#17324d"
BLUE = "#2667c9"


def configure_dpi():
    """Use physical client coordinates for crisp Windows high-DPI rendering."""
    import os
    if os.name == "nt":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


def style(root):
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure("TFrame", background=BG)
    s.configure("TLabel", background=BG, foreground=INK, font=("Microsoft YaHei UI", 10))
    s.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
    s.configure("Hint.TLabel", foreground="#52677e", font=("Microsoft YaHei UI", 9))
    s.configure("Card.TLabelframe", background=BG, padding=12)
    s.configure("Card.TLabelframe.Label", foreground=INK, background=BG, font=("Microsoft YaHei UI", 11, "bold"))
    s.configure("TButton", padding=(12, 7), font=("Microsoft YaHei UI", 10), background="#e4ecf7", foreground=INK, borderwidth=0)
    s.map("TButton", background=[("active", "#d4e2f5")])
    s.configure("Primary.TButton", background=BLUE, foreground="white")
    s.map("Primary.TButton", background=[("active", "#1752a7"), ("disabled", "#a6b5c5")])
    s.configure("TNotebook", background=BG, borderwidth=0)
    s.configure("TCheckbutton", background=BG, foreground=INK)
    s.configure("TRadiobutton", background=BG, foreground=INK)
    s.configure("Horizontal.TProgressbar", background=BLUE, troughcolor="#e4ecf7", borderwidth=0)
    s.configure("TNotebook.Tab", padding=(12, 8), font=("Microsoft YaHei UI", 10), background="#e4ecf7")
    s.map("TNotebook.Tab", background=[("selected", "#ffffff"), ("active", "#d4e2f5")], foreground=[("selected", BLUE)])
    s.configure("Treeview", rowheight=28, font=("Microsoft YaHei UI", 9), background="white", fieldbackground="white")
    s.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"), padding=7, background="#e4ecf7", foreground=INK)
    root.configure(background=BG)


class ScrollPage(ttk.Frame):
    """Scrollable forms remain reachable on ordinary laptop displays."""
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, background=BG, highlightthickness=0)
        bar = ttk.Scrollbar(self, command=self.canvas.yview)
        self.horizontal = ttk.Scrollbar(self, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=bar.set, xscrollcommand=self.horizontal.set)
        self.body = ttk.Frame(self.canvas, padding=18)
        self._min_body_width=0
        self.window = self.canvas.create_window((0, 0), anchor="nw", window=self.body)
        self.canvas.grid(row=0,column=0,sticky='nsew')
        bar.grid(row=0,column=1,sticky='ns')
        self.horizontal.grid(row=1,column=0,sticky='ew')
        self.rowconfigure(0,weight=1); self.columnconfigure(0,weight=1)
        self.body.bind("<Configure>", self._layout)
        self.canvas.bind("<Configure>", self._layout)
        self._wheel_remainder=0
        # Tk events do not bubble through Frame parents. A toplevel binding
        # scoped to this page lets labels/entries scroll without stealing Text,
        # table or plot scrolling, or altering another window's bindings.
        self._top=self.winfo_toplevel()
        self._wheel_bindings={sequence:self._top.bind(sequence,self._wheel,add='+')
                              for sequence in ('<MouseWheel>','<Button-4>','<Button-5>')}
        self.bind('<Destroy>',self._cleanup,add='+')

    def _layout(self,event=None):
        # Wrapped labels can alternately request two widths after allocation.
        # Retain the natural minimum rather than feeding that oscillation back
        # into the canvas (which otherwise never finishes update_idletasks).
        self._min_body_width=max(self._min_body_width,self.body.winfo_reqwidth())
        width=max(self.canvas.winfo_width(),self._min_body_width)
        if int(float(self.canvas.itemcget(self.window,'width') or 0))!=width:
            self.canvas.itemconfigure(self.window,width=width)
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))
        if width>self.canvas.winfo_width()+2:self.horizontal.grid()
        else:self.horizontal.grid_remove()

    def _wheel(self,event):
        widget=event.widget
        while widget is not self:
            if isinstance(widget,(tk.Text,ttk.Treeview,tk.Listbox,ttk.Combobox)):
                return
            if isinstance(widget,tk.Canvas) and widget is not self.canvas:return
            widget=getattr(widget,'master',None)
            if widget is None:return
        if not self.winfo_ismapped():return
        button=getattr(event,'num',None)
        delta=120 if button==4 else -120 if button==5 else getattr(event,'delta',0)
        self._wheel_remainder+=delta
        units=int(self._wheel_remainder/120)
        self._wheel_remainder-=units*120
        if units:self.canvas.yview_scroll(-units,'units')
        return 'break'

    def _cleanup(self,event):
        if event.widget is not self:return
        for sequence,identifier in self._wheel_bindings.items():
            try:self._top.unbind(sequence,identifier)
            except tk.TclError:pass


def card(parent, title):
    box = ttk.LabelFrame(parent, text=title, style="Card.TLabelframe")
    box.pack(fill="x", pady=(0, 12))
    return box


def hint(parent, text):
    label = ttk.Label(parent, text=text, style="Hint.TLabel", wraplength=1050, justify="left")
    label.pack(anchor="w", fill="x", pady=(4, 8))
    parent.bind("<Configure>", lambda e: label.configure(wraplength=max(220, e.width - 30)), add="+")
    return label


def field(parent, row, col, label, variable, *, values=None, width=22):
    frame = ttk.Frame(parent)
    frame.grid(row=row, column=col, sticky="ew", padx=(0, 16), pady=5)
    parent.columnconfigure(col, weight=1)
    ttk.Label(frame, text=label, wraplength=250).pack(anchor="w", pady=(0, 3))
    entry = (ttk.Combobox(frame, textvariable=variable, values=values, state="readonly", width=width)
             if values is not None else ttk.Entry(frame, textvariable=variable, width=width))
    entry.pack(fill="x")
    return entry


class ImageView(ttk.Frame):
    def __init__(self, parent, height=410):
        super().__init__(parent)
        self.image = None
        self.path = None
        self.label = ttk.Label(self, text="生成结构后显示截面和图例", anchor="center")
        self.label.pack(fill="both", expand=True)
        self.height = height
        self.bind("<Configure>", self._resize)

    def clear(self, text="生成结构后显示截面和图例"):
        self.path=None
        self.image=None
        if hasattr(self,'original'):del self.original
        self.label.configure(image='',text=text)

    def show(self, path):
        from PIL import Image
        self.path = Path(path)
        with Image.open(path) as im:
            self.original = im.copy()
        self._resize()

    def _resize(self, event=None):
        if not hasattr(self, "original"):
            return
        from PIL import Image, ImageTk
        copy = self.original.copy()
        copy.thumbnail((max(300, self.winfo_width()), self.height), Image.Resampling.LANCZOS)
        self.image = ImageTk.PhotoImage(copy, master=self)
        self.label.configure(image=self.image, text="")


def make_modal(window, parent):
    """Keep an editor attached to its originating project/window until closed."""
    window.transient(parent.winfo_toplevel())
    window.grab_set()
    window.bind('<Escape>',lambda event:window.destroy())


class JsonDialog(tk.Toplevel):
    """Advanced editing complements the primary forms, never replaces them."""
    def __init__(self, parent, title, value, apply):
        super().__init__(parent)
        self.title(title)
        self.geometry("850x650")
        make_modal(self,parent)
        body=ttk.Frame(self,padding=12);body.pack(fill='both',expand=True)
        body.columnconfigure(0,weight=1);body.rowconfigure(0,weight=1)
        self.text = tk.Text(body, undo=True, font=("Consolas", 10), wrap="none")
        self.text.grid(row=0,column=0,sticky='nsew')
        vertical=ttk.Scrollbar(body,command=self.text.yview)
        horizontal=ttk.Scrollbar(body,orient='horizontal',command=self.text.xview)
        vertical.grid(row=0,column=1,sticky='ns');horizontal.grid(row=1,column=0,sticky='ew')
        self.text.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        self.text.insert("1.0", json.dumps(value, ensure_ascii=False, indent=2))
        self.error = tk.StringVar()
        ttk.Label(self, textvariable=self.error, foreground="#b23939", wraplength=800).pack(fill="x", padx=12)
        def commit():
            try:
                apply(json.loads(self.text.get("1.0", "end")))
                self.destroy()
            except Exception as exc:
                self.error.set(str(exc))
        bar=ttk.Frame(self,padding=12);bar.pack(fill='x')
        ttk.Button(bar,text='取消',command=self.destroy).pack(side='right',padx=6)
        ttk.Button(bar, text="应用修改", command=commit).pack(side='right')
        self.bind('<Control-Return>',lambda event:commit())
