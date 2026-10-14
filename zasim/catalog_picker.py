"""Keyboard-accessible, owned modal catalog picker; selection never saves a file."""

from .i18n import tr
from .ui_catalog import search


class CatalogPicker:
    LIMIT = 200

    def __init__(self, parent, *, tk, ttk, kind, title, accept,
                 initial_value=None, initial_form=None):
        search(kind, "")  # Validate resources before creating any modal UI.
        self.tk, self.kind, self.accept = tk, kind, accept
        self.window = w = tk.Toplevel(parent)
        w.title(title)
        w.transient(parent)
        w.geometry(
            f"{min(640, max(320, parent.winfo_width()))}x{min(520, max(280, parent.winfo_height()))}"
        )
        w.minsize(320, 240)
        w.columnconfigure(0, weight=1)
        w.rowconfigure(1, weight=1)
        head = ttk.Frame(w, padding=10)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        self.search_label = ttk.Label(head, text=tr("名前・番号で検索"), wraplength=280)
        self.search_label.grid(row=0, column=0, columnspan=2, sticky="ew")
        head.bind("<Configure>", lambda event: self.search_label.configure(
            wraplength=max(120, event.width - 20)), add="+")
        self.query = tk.StringVar(master=w)
        self.entry = ttk.Entry(head, textvariable=self.query, width=18)
        self.entry.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.clear = ttk.Button(
            head, text=tr("クリア"), command=lambda: self.query.set("")
        )
        self.clear.grid(row=1, column=1, padx=(6, 0), pady=(4, 0))
        body = ttk.Frame(w, padding=(10, 0))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        from tkinter import font

        text_font = font.nametofont("TkDefaultFont", root=w)
        self._text_font = text_font
        ttk.Style(w).configure(
            "Catalog.Treeview", rowheight=text_font.metrics("linespace") + 10
        )
        number_width = max(
            75, text_font.measure("2685") + 20, text_font.measure(tr("番号")) + 20
        )
        self.tree = ttk.Treeview(
            body,
            columns=("name", "number"),
            show="headings",
            selectmode="browse",
            height=6,
            style="Catalog.Treeview",
        )
        self.tree.heading("name", text=tr("名前"))
        self.tree.heading("number", text=tr("番号"))
        self.tree.column("name", width=210, minwidth=100)
        self.tree.column(
            "number", width=number_width, minwidth=number_width, stretch=False
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(body, command=self.tree.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=bar.set)
        self.horizontal = ttk.Scrollbar(body, orient="horizontal", command=self.tree.xview)
        self.horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(xscrollcommand=self._horizontal_view)
        footer = ttk.Frame(w, padding=10)
        footer.grid(row=2, column=0, sticky="ew")
        paging = ttk.Frame(footer)
        paging.pack(fill="x")
        self.previous_button = ttk.Button(paging, text=tr("前へ"), width=0,
                                          command=lambda: self._turn_page(-1))
        self.previous_button.pack(side="left")
        self.next_button = ttk.Button(paging, text=tr("次へ"), width=0,
                                      command=lambda: self._turn_page(1))
        self.next_button.pack(side="right")
        self.count = ttk.Label(paging, style="Hint.TLabel", wraplength=240, anchor="center")
        self.count.pack(side="left", fill="x", expand=True, padx=6)
        actions = ttk.Frame(footer)
        actions.pack(fill="x", pady=(6, 0))
        self.choose = ttk.Button(
            actions, text=tr("選択"), style="Accent.TButton", command=self.select
        )
        self.choose.pack(side="right")
        self.cancel = ttk.Button(actions, text=tr("キャンセル"), command=w.destroy)
        self.cancel.pack(side="right", padx=(0, 6))
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.tree.bind("<Double-1>", self._double_click)
        w.bind("<Return>", lambda _: self.select())
        w.bind("<Escape>", lambda _: w.destroy())
        w.bind("<Alt-Left>", lambda _: self._turn_page(-1))
        w.bind("<Alt-Right>", lambda _: self._turn_page(1))
        self.entry.bind("<Down>", self._first)
        self._trace = self.query.trace_add("write", lambda *_: self.filter())
        w.bind("<Destroy>", self._destroyed, add="+")
        self.page = 0
        self.filter()
        if initial_value is not None:
            for index, choice in enumerate(self.result):
                if choice.value == initial_value and (initial_form is None or choice.form == initial_form):
                    self.page = index // self.LIMIT
                    self._render()
                    row = str(index % self.LIMIT)
                    self.tree.selection_set(row)
                    self.tree.focus(row)
                    self.tree.see(row)
                    self._selected()
                    break
        w.grab_set()
        self.entry.focus_set()

    def filter(self):
        self.result = search(self.kind, self.query.get())
        self.page = 0
        self._render()

    def _render(self):
        start = self.page * self.LIMIT
        self.rows = self.result[start:start + self.LIMIT]
        self.tree.delete(*self.tree.get_children())
        width = 100
        for index, row in enumerate(self.rows):
            label = row.label
            if row.form is not None and row.form:
                label += tr("（フォルム {v0}）", v0=row.form)
            self.tree.insert("", "end", iid=str(index), values=(label, row.number))
            width = max(width, self._text_font.measure(label) + 20)
        self.tree.column("name", width=width, minwidth=width)
        self.choose.state(["disabled"])
        self.count.configure(
            text=(
                tr("候補がありません。検索語を変えてください。")
                if not self.result
                else f"{start + 1}–{start + len(self.rows)} / {len(self.result)}"
            )
        )
        self.previous_button.state(["!disabled"] if self.page else ["disabled"])
        self.next_button.state(["!disabled"] if start + self.LIMIT < len(self.result) else ["disabled"])
        self.tree.yview_moveto(0)
        self.tree.xview_moveto(0)

    def _horizontal_view(self, first, last):
        self.horizontal.set(first, last)
        if float(first) <= 0 and float(last) >= 1:
            self.horizontal.grid_remove()
        else:
            self.horizontal.grid()

    def _turn_page(self, delta):
        last = max(0, (len(self.result) - 1) // self.LIMIT)
        page = min(last, max(0, self.page + delta))
        if page != self.page:
            self.page = page
            self._render()
        return "break"

    def _double_click(self, event):
        row = self.tree.identify_row(event.y)
        if row and row in self.tree.selection():
            self.select()
        return "break"

    def _first(self, _event=None):
        if self.rows:
            self.tree.selection_set("0")
            self.tree.focus("0")
            self.tree.focus_set()
        return "break"

    def _selected(self, _event=None):
        self.choose.state(["!disabled"] if self.tree.selection() else ["disabled"])

    def select(self):
        selection = self.tree.selection()
        if selection and self.accept(self.rows[int(selection[0])]):
            self.window.destroy()
        return "break"

    def _destroyed(self, event):
        if event.widget is self.window and self._trace is not None:
            token, self._trace = self._trace, None
            try:
                self.query.trace_remove("write", token)
            except self.tk.TclError:
                pass
