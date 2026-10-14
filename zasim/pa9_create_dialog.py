"""Compact, searchable new-entity form. No file/save/backend operations."""
from .i18n import tr, get_language
from datetime import date
import secrets

from .pa9_creation import CreateRequest, LANGUAGES, STARTERS, species_catalog
from .pa9_editor import PA9EditorDocument
from .text import display_safe
from .ui_catalog import normalize_query


class NewPA9Dialog:
    def __init__(self, parent, *, tk, ttk, messagebox, scroll_class, accept):
        self.tk, self.ttk, self.messagebox, self.accept = tk, ttk, messagebox, accept
        self.window = w = tk.Toplevel(parent)
        w.title(tr('新規PA9 — 元セーブ不要'))
        w.transient(parent)
        w.geometry(f"{min(720, max(320, parent.winfo_width()))}x{min(760, max(300, parent.winfo_height()))}")
        w.minsize(320, 260)
        w.columnconfigure(0, weight=1)
        w.rowconfigure(0, weight=1)
        self.area = area = scroll_class(w, tk=tk, ttk=ttk)
        area.frame.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        body = area.content
        body.columnconfigure(0, weight=1)
        self.vars, self.widgets, self.labels = {}, {}, []
        self.field_labels, self.field_groups, self._responsive_sets = {}, {}, []
        self.catalog = species_catalog()
        name_language = "1" if get_language() == "ja" else "2"
        self.species_labels = {f'{p["species"]}:{p["form"]}  {p["names"][name_language]}': p for p in self.catalog}
        self._search_text = {label: normalize_query(label + " " + " ".join(p["names"].values()))
                             for label, p in self.species_labels.items()}
        self.search_var = tk.StringVar(master=w)
        self.optional_var = tk.BooleanVar(master=w, value=False)

        model = ttk.LabelFrame(body, text=tr('個体'), padding=10)
        model.grid(row=0, column=0, sticky="ew")
        model.columnconfigure(0, weight=1)
        model_fields = ttk.Frame(model)
        model_fields.grid(row=0, column=0, sticky="ew")
        search = ttk.Frame(model)
        search.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        search.columnconfigure(0, weight=1)
        ttk.Label(search, text=tr('種族を検索（名前・全国番号）'), style="Hint.TLabel").grid(row=0, column=0, sticky="w")
        self.search_entry = ttk.Entry(search, textvariable=self.search_var, width=18)
        self.search_entry.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        self.search_clear_button = ttk.Button(search, text=tr('クリア'), command=lambda: self.search_var.set(""))
        self.search_clear_button.grid(row=1, column=1, padx=(6, 0), pady=(3, 0))
        self.search_count = ttk.Label(search, style="Hint.TLabel")
        self.search_count.grid(row=2, column=0, columnspan=2, sticky="w", pady=(3, 0))
        species_row = ttk.Frame(model)
        species_row.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self.custom_fields = ttk.Frame(model)
        self.custom_fields.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        trainer = ttk.LabelFrame(body, text=tr('トレーナー'), padding=10)
        trainer.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        optional_toggle = ttk.Checkbutton(body, text=tr('詳細（任意）'), variable=self.optional_var, command=self._toggle_optional)
        optional_toggle.grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.optional_toggle = optional_toggle
        self.optional_fields = ttk.Frame(body, padding=(6, 4))
        self.optional_fields.grid(row=3, column=0, sticky="ew")
        self._encounter_codes = {tr('御三家ギフト'): "starter", tr('条件を自分で指定'): "custom"}
        self._language_codes = dict(zip(("日本語", "English", "Français", "Italiano", "Deutsch", "Español",
            "한국어", "简体中文", "繁體中文", "Español (Latinoamérica)"), LANGUAGES))
        self._gender_codes = {tr('男'): 0, tr('女'): 1}
        definitions = (
            ("encounter", tr('作成方法'), tr('御三家ギフト'), tuple(self._encounter_codes), model_fields),
            ("species", tr('種族（全国番号:フォルム）'), "", (), species_row),
            ("level", tr('現在Lv（1–100）'), "5", None, model_fields),
            ("ot_name", tr('トレーナー名（必須）'), "", None, trainer),
            ("tid", tr('トレーナーID (TID16)'), "", None, trainer),
            ("sid", "SID16（0–65535）", "", None, trainer),
            ("language", tr('個体の言語'), "日本語" if get_language() == "ja" else "English", tuple(self._language_codes), trainer),
            ("ot_gender", tr('親の性別'), tr('男'), tuple(self._gender_codes), self.optional_fields),
            ("met_date", tr('出会い日（YYYY-MM-DD）'), date.today().isoformat(), None, self.optional_fields),
            ("met_location", tr('カスタム: 出会い場所ID（必須）'), "", None, self.custom_fields),
            ("met_level", tr('カスタム: 出会いLv（必須）'), "", None, self.custom_fields),
            ("ball", tr('ボールID（starterは4固定）'), "4", None, self.custom_fields),
            ("nickname", tr('ニックネーム（空欄=種族名）'), "", None, self.optional_fields),
            ("moves", tr('技IDs（カンマ区切り／空欄=自動）'), "", None, self.optional_fields),
        )
        grouped = {}
        for name, label, default, choices, section in definitions:
            group = ttk.Frame(section)
            group.columnconfigure(0, weight=1)
            grouped.setdefault(section, []).append(group)
            self.field_groups[name] = group
            var = self.vars[name] = tk.StringVar(master=w, value=default)
            lab = ttk.Label(group, text=label, wraplength=250, style="Hint.TLabel")
            lab.grid(row=0, column=0, sticky="ew", pady=(0, 3))
            self.labels.append(lab)
            self.field_labels[name] = lab
            group.bind("<Configure>", lambda e, label=lab: label.configure(wraplength=max(100, e.width - 4)), add="+")
            widget = (ttk.Entry(group, textvariable=var, width=18) if choices is None
                      else ttk.Combobox(group, textvariable=var, values=choices, state="readonly", width=18))
            widget.grid(row=1, column=0, sticky="ew")
            widget.bind("<FocusIn>", lambda e: area.see(e.widget), add="+")
            self.widgets[name] = widget
        id_group = ttk.Frame(trainer)
        self.generate_ids_button = ttk.Button(id_group, text=tr('新しいIDを生成'), command=self._generate_ids)
        self.generate_ids_button.pack(anchor="w")
        ttk.Label(id_group, text=tr('ゲームのIDを複製せず、新規IDを入力します。'),
                  style="Hint.TLabel", wraplength=240).pack(fill="x", pady=(3, 0))
        grouped[trainer].append(id_group)
        for section, groups in grouped.items():
            self._responsive_sets.append((section, groups, 1 if section is species_row else 2))
        self.notice = ttk.Label(body, text="", wraplength=280, style="Hint.TLabel")
        self.notice.grid(row=4, column=0, sticky="ew", pady=(10, 4))
        area.canvas.bind("<Configure>", self._wrap, add="+")
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            w.bind(event, self._wheel, add="+")
        footer = ttk.Frame(w, padding=(12, 8))
        footer.grid(row=1, column=0, sticky="ew")
        self.cancel_button = ttk.Button(footer, text=tr('キャンセル'), command=w.destroy)
        self.cancel_button.pack(side="right")
        self.create_button = ttk.Button(footer, text=tr('作成して編集'), style="Accent.TButton", command=self.submit)
        self.create_button.pack(side="right", padx=(0, 8))
        self._input_traces = [(var, var.trace_add("write", lambda *_args, field=name: self._input_changed(field)))
                              for name, var in self.vars.items() if name != "encounter"]
        self._mode_trace = self.vars["encounter"].trace_add("write", lambda *_: self._mode())
        self._search_trace = self.search_var.trace_add("write", lambda *_: self._filter_species())
        w.bind("<Destroy>", self._destroyed, add="+")
        self._mode()
        self._toggle_optional()
        w.bind("<Escape>", lambda _event: w.destroy())
        w.grab_set()
        self._wrap()
        self.search_entry.focus_set()

    def _destroyed(self, event):
        if event.widget is not self.window:
            return
        for variable, callback in self._input_traces:
            try:
                variable.trace_remove("write", callback)
            except self.tk.TclError:
                pass
        self._input_traces.clear()
        # Remove only this dialog's registrations; foreign traces survive.
        for variable, attribute in ((self.vars["encounter"], "_mode_trace"), (self.search_var, "_search_trace")):
            callback = getattr(self, attribute)
            setattr(self, attribute, None)
            if callback is not None:
                try:
                    variable.trace_remove("write", callback)
                except self.tk.TclError:
                    pass

    def _input_changed(self, name):
        widget = self.widgets[name]
        if widget.winfo_exists():
            widget.state(["!invalid"])

    def _wrap(self, _event=None):
        width = max(180, self.area.canvas.winfo_width() - 36)
        for section, groups, maximum in self._responsive_sets:
            cell = max(widget.winfo_reqwidth() for widget in self.widgets.values()
                       if widget.master in groups) + 12
            columns = min(maximum, max(1, width // max(220, cell)))
            for column in range(maximum):
                section.columnconfigure(column, weight=1 if column < columns else 0,
                                        uniform="fields" if column < columns else "")
            for index, group in enumerate(groups):
                group.grid(row=index // columns, column=index % columns, sticky="nsew", padx=4, pady=5)
        self.notice.configure(wraplength=width)

    def _toggle_optional(self):
        self.optional_fields.grid() if self.optional_var.get() else self.optional_fields.grid_remove()

    def _wheel(self, event):
        if event.widget.winfo_class() in ("TCombobox", "Listbox"):
            return None
        amount = -1 if getattr(event, "num", 0) == 4 or getattr(event, "delta", 0) > 0 else 1
        self.area.canvas.yview_scroll(amount * 3, "units")
        return "break"

    def _encounter_mode(self):
        value = self.vars["encounter"].get()
        return self._encounter_codes.get(value, value.split(":", 1)[0])

    def _allowed_species(self):
        starter = self._encounter_mode() == "starter"
        return [label for label, p in self.species_labels.items()
                if not starter or (p["species"] in STARTERS and p["form"] == 0)]

    def _filter_species(self):
        query = normalize_query(self.search_var.get().strip())
        choices = [label for label in self._allowed_species() if query in self._search_text[label]]
        self.widgets["species"].configure(values=choices)
        self.search_count.configure(text=tr('候補: {v0}件', v0=len(choices)))
        # Filtering changes only the menu, never the explicitly displayed choice.

    def _mode(self):
        self._input_changed("encounter")
        starter = self._encounter_mode() == "starter"
        choices = self._allowed_species()
        if self.vars["species"].get() not in choices:
            self.vars["species"].set(next((label for label in choices if label.startswith("152:0 ")), choices[0]))
        if self.search_var.get():
            self.search_var.set("")
        else:
            self._filter_species()
        for name in ("met_location", "met_level", "ball"):
            self.widgets[name].state(["disabled"] if starter else ["!disabled"])
        self.custom_fields.grid_remove() if starter else self.custom_fields.grid()
        self.notice.configure(text=(
            tr('starter: 御三家6種のギフト。出会いLv5・場所・ボールを固定。能力値と技を計算します。')
            if starter else tr('custom: 出会い条件は利用者が指定する下書きです。正規入手可能性は検証しません。')
        ) + tr(' 合法性の完全判定・ゲーム読込・実機交換を保証しません。'))

    def _generate_ids(self):
        self.vars["tid"].set(str(secrets.randbelow(65536)))
        self.vars["sid"].set(str(secrets.randbelow(65536)))

    def _focus_error(self, name):
        widget = self.widgets[name]
        if name in ("met_date", "nickname", "moves", "ot_gender"):
            self.optional_var.set(True)
            self._toggle_optional()
        self.window.update_idletasks()
        self.area.see(widget)
        widget.state(["invalid"])
        widget.focus_set()

    def request(self):
        v = {name: var.get() for name, var in self.vars.items()}
        if not v["ot_name"].strip():
            self._focus_error("ot_name")
            raise ValueError(tr('トレーナー名を入力してください。'))
        try:
            p = self.species_labels[v["species"]]
        except KeyError:
            self._focus_error("species")
            raise ValueError(tr('一覧から種族を選んでください。')) from None
        v["encounter"] = self._encounter_mode()
        starter = v["encounter"] == "starter"
        def integer(name, text=None):
            text = v[name] if text is None else text
            if name == "language":
                text = str(self._language_codes.get(text, text))
            elif name == "ot_gender":
                text = str(self._gender_codes.get(text, text))
            if name in ("language", "ot_gender"):
                text = text.split(":", 1)[0]
            try:
                return int(text, 16) if text.lower().startswith("0x") else int(text, 10)
            except ValueError:
                self._focus_error(name)
                raise ValueError(tr('「{v0}」には整数を入力してください。',
                                    v0=self.field_labels[name].cget("text"))) from None
        try:
            met_date = date.fromisoformat(v["met_date"])
        except ValueError:
            self._focus_error("met_date")
            raise ValueError(tr('出会い日は YYYY-MM-DD 形式で入力してください。')) from None
        return CreateRequest(
            species=p["species"], form=p["form"], level=integer("level"),
            ot_name=v["ot_name"], tid=integer("tid"), sid=integer("sid"),
            language=integer("language"), ot_gender=integer("ot_gender"),
            encounter=v["encounter"], met_date=met_date,
            met_location=None if starter else integer("met_location"),
            met_level=None if starter else integer("met_level"),
            ball=4 if starter else integer("ball"), nickname=v["nickname"] or None,
            moves=tuple(integer("moves", x.strip()) for x in v["moves"].split(",")) if v["moves"] else None,
        )

    def submit(self):
        try:
            document = PA9EditorDocument.create(self.request())
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            message = str(exc)
            for prefix, field in (("TID16", "tid"), ("SID16", "sid"), ("OT name", "ot_name"),
                                  ("nickname", "nickname"), ("met date", "met_date"),
                                  ("current level", "level"), ("level", "level"), ("move ", "moves")):
                if message.startswith(prefix):
                    self._focus_error(field)
                    break
            self.messagebox.showerror(tr('新規PA9を作成できません'), display_safe(exc, limit=2048), parent=self.window)
            return
        if self.accept(document):
            self.window.destroy()
