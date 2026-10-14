"""Cross-platform tkinter desktop UI for za-ldn-trade.

tkinter is imported only by :func:`launch_gui`, so every non-GUI CLI command
and the headless test suite continue to work on servers without Tk/display.
"""

from __future__ import annotations

from .i18n import (tr, set_language, preferred_language,
                   save_language, validate_language, LANGUAGE_NAMES)

import json
import os
from collections import deque
from dataclasses import replace
from pathlib import Path
import queue
import shutil
import sys
import threading
import time
from typing import Any
from . import __version__

from .capture import analyze_capture, capture_file_identity
from .compatibility import (
    DEFAULT_ZA_TITLE_VERSION, SUPPORTED_ZA_TITLE_VERSIONS, identification_token_for_title,
)
from .client import LiveTradeResult
from .control import OperationCancelled, raise_if_cancelled
from .gui_controller import (
    ConnectionSettings,
    ConfirmationPrompt,
    ControllerState,
    GuiController,
    GuiEvent,
    ProbeRequest,
    TradeRequest,
)
from .pa9 import PA9, is_encrypted_pa9
from .file_save import read_regular_bytes
from .pa9_editor import (
    EDITABLE_FIELDS,
    FIELD_SPECS,
    GENDER_NAMES_JA,
    NATURE_NAMES_JA,
    PA9EditorDocument,
    apply_pa9_changes,
    parse_editor_value,
    snapshot_pa9_output_identity,
    validate_editor_record,
)
from .text import display_safe
from .ui_catalog import field_catalog, display_value, display_input_id
from .radio_picker import RadioChoice, RadioInventory, read_inventory, resolve_choice


MAX_GUI_ANALYSIS_RECORDS = 1000


def _pa9_summary(path: Path) -> dict[str, object]:
    size = path.stat().st_size
    if size not in {PA9.STORED_SIZE, PA9.PARTY_SIZE}:
        raise ValueError(
            f"PA9 file must contain exactly 0x{PA9.STORED_SIZE:X} or "
            f"0x{PA9.PARTY_SIZE:X} bytes (got {size})"
        )
    raw = read_regular_bytes(path, max_size=PA9.PARTY_SIZE, allow_symlink=True)
    encrypted = is_encrypted_pa9(raw)
    record = PA9(raw, encrypted=encrypted)
    return {
        "path": str(path),
        "bytes": len(raw),
        "encrypted": encrypted,
        "species_internal": record.species_internal,
        "nickname": display_safe(record.nickname, limit=64),
        "level": record.stat_level if record.is_party else None,
        "party_stats_present": record.party_stats_present,
        "checksum_valid": record.checksum_valid,
        "encryption_constant": f"0x{record.ec:08X}",
        "pid": f"0x{record.pid:08X}",
        "tid": record.tid,
        "sid": record.sid,
    }


def _collect_capture_analysis(
    path: Path,
    *,
    preview: int,
    cancel_event: threading.Event,
    limit: int = MAX_GUI_ANALYSIS_RECORDS,
) -> list[dict[str, object]]:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("analysis record limit must be a positive integer")
    records: list[dict[str, object]] = []
    raise_if_cancelled(cancel_event)
    for record in analyze_capture(path, preview=preview, cancel_event=cancel_event):
        raise_if_cancelled(cancel_event)
        if len(records) >= limit:
            records.append(
                {
                    "rec": "gui-notice",
                    "truncated": True,
                    "message": (
                        f"GUI preview stopped after {limit} records; use the CLI "
                        "analyze command for streaming full output"
                    ),
                }
            )
            break
        records.append(record)
    raise_if_cancelled(cancel_event)
    return records


class _ScrollArea:
    """A bounded viewport, not a smaller/clipped form. No global event bindings."""

    def __init__(self, parent: Any, *, tk: Any, ttk: Any, height: int = 300) -> None:
        self.frame = ttk.Frame(parent)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)
        background = ttk.Style(parent).lookup("TFrame", "background")
        self.canvas = tk.Canvas(
            self.frame, width=1, height=height, borderwidth=0,
            highlightthickness=0, background=background, takefocus=True,
            xscrollincrement=1, yscrollincrement=1,
        )
        self.vertical = ttk.Scrollbar(self.frame, command=self.canvas.yview)
        self.horizontal = ttk.Scrollbar(
            self.frame, orient="horizontal", command=self.canvas.xview
        )
        self.canvas.configure(
            yscrollcommand=self.vertical.set, xscrollcommand=self._horizontal_view
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vertical.grid(row=0, column=1, sticky="ns")
        self.horizontal.grid(row=1, column=0, sticky="ew")
        self.content = ttk.Frame(self.canvas)
        self.window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._resize)
        self.canvas.bind("<Configure>", self._resize)
        for key, axis, amount in (
            ("<Prior>", "y", -1), ("<Next>", "y", 1),
            ("<Left>", "x", -1), ("<Right>", "x", 1),
        ):
            self.canvas.bind(key, lambda _e, a=axis, n=amount: self._page(a, n))

    def _horizontal_view(self, first: str, last: str) -> None:
        self.horizontal.set(first, last)
        if float(first) <= 0 and float(last) >= 1:
            self.horizontal.grid_remove()
        else:
            self.horizontal.grid()

    def _page(self, axis: str, amount: int) -> str:
        getattr(self.canvas, axis + "view_scroll")(amount, "pages")
        return "break"

    def _resize(self, _event: Any = None) -> None:
        width = max(self.canvas.winfo_width(), self.content.winfo_reqwidth())
        height = max(self.canvas.winfo_height(), self.content.winfo_reqheight())
        self.canvas.itemconfigure(self.window, width=width, height=height)
        self.canvas.configure(scrollregion=(0, 0, width, height))

    def contains(self, widget: Any) -> bool:
        while widget is not None:
            if widget is self.frame:
                return True
            widget = getattr(widget, "master", None)
        return False

    def see(self, widget: Any) -> None:
        """Reveal a keyboard-focused control, including nested editor pages."""
        if not self.contains(widget) or widget is self.canvas:
            return
        # A nested canvas may just have scrolled. Settle its window geometry
        # before the outer viewport measures the same focused control.
        self.canvas.update_idletasks()
        region = self.canvas.bbox(self.window)
        if not region:
            return
        for axis, start, size, viewport, total in (
            ("x", widget.winfo_rootx() - self.content.winfo_rootx(),
             widget.winfo_width(), self.canvas.winfo_width(), region[2]),
            ("y", widget.winfo_rooty() - self.content.winfo_rooty(),
             widget.winfo_height(), self.canvas.winfo_height(), region[3]),
        ):
            view = getattr(self.canvas, axis + "view")
            offset = view()[0] * total
            if start < offset:
                getattr(self.canvas, axis + "view_moveto")(max(0, start - 4) / total)
            elif start + size > offset + viewport:
                getattr(self.canvas, axis + "view_moveto")(
                    max(0, min(start, start + size - viewport + 4)) / total
                )


class _DesktopApp:
    POLL_MS = 50
    BACKLOG_POLL_MS = 1
    POLL_BUDGET_SECONDS = 0.008
    LOG_SLICE_LINES = 64
    LOG_SLICE_CHARS = 32768
    MAX_LOG_LINES = 5000

    def __init__(
        self,
        root: Any,
        *,
        tk: Any,
        ttk: Any,
        filedialog: Any,
        messagebox: Any,
        scrolledtext: Any,
        backend: str,
        relay: str,
        keys: str,
        phyname: str,
        ifname: str,
        nickname: str,
        title_version: str = DEFAULT_ZA_TITLE_VERSION,
        language: str = "ja",
    ) -> None:
        self.language = validate_language(language)
        set_language(self.language)
        self.root = root
        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.scrolledtext = scrolledtext
        self.controller = GuiController()
        self.active_job_id: int | None = None
        self.active_operation: str | None = None
        self._pending_terminal_event: GuiEvent | None = None
        self._pending_controller_events: deque[GuiEvent] = deque()
        self._pending_log_lines: deque[str] = deque()
        self._pending_offline_event: tuple[str, object] | None = None
        self.close_when_idle = False
        self._offline_thread: threading.Thread | None = None
        self._offline_cancel: threading.Event | None = None
        self._offline_events: queue.SimpleQueue[
            tuple[str, object]
        ] = queue.SimpleQueue()
        self._log_lines = 0

        root.title(f"za-ldn-trade {__version__}")
        # Do not force a 900x680 minimum onto a small desktop. Respect OS fonts/DPI.
        width = min(1080, max(1, root.winfo_screenwidth() - 48))
        height = min(820, max(1, root.winfo_screenheight() - 80))
        root.geometry(f"{width}x{height}")
        root.minsize(min(360, width), min(240, height))
        root.protocol("WM_DELETE_WINDOW", self._request_close)
        root.bind("<Destroy>", self._root_destroyed, add="+")
        root.bind("<Control-q>", lambda _event: self._request_close())

        self._configure_style()
        self.task_var = tk.StringVar(master=self.root, value="0")
        self._active_page = None
        self.language_var = tk.StringVar(master=self.root, value=LANGUAGE_NAMES[self.language])

        self.backend_var = tk.StringVar(master=self.root, value=backend)
        self.relay_var = tk.StringVar(master=self.root, value=relay)
        self.keys_var = tk.StringVar(master=self.root, value=keys)
        self.phy_var = tk.StringVar(master=self.root, value=phyname)
        self.interface_var = tk.StringVar(master=self.root, value=ifname)
        self.nickname_var = tk.StringVar(master=self.root, value=nickname)
        self.title_version_var = tk.StringVar(master=self.root, value=title_version)
        self.single_interface_var = tk.StringVar(master=self.root, value=tr('自動'))
        self.bss_helper_var = tk.StringVar(master=self.root)
        self._bss_helper_previous_mode = "自動"
        self.link_code_var = tk.StringVar(master=self.root)
        self.connection_advanced_var = tk.BooleanVar(master=self.root, value=False)
        self.radio_var = tk.StringVar(master=self.root)
        self._radio_inventory = RadioInventory("not_loaded")
        self._selected_radio: RadioChoice | None = None
        self._initial_radio_hint = (phyname, ifname) if phyname else None
        self.status_var = tk.StringVar(master=self.root, value=tr('待機中'))

        self.offer_var = tk.StringVar(master=self.root)
        self.output_var = tk.StringVar(master=self.root)
        self.timeout_var = tk.StringVar(master=self.root, value="300")
        self.settle_var = tk.StringVar(master=self.root, value="5")
        self.auto_confirm_var = tk.BooleanVar(master=self.root, value=False)

        self.capture_output_var = tk.StringVar(master=self.root, value=str(Path.cwd() / "za-capture.jsonl"))
        self.capture_duration_var = tk.StringVar(master=self.root, value="30")

        self.inspect_var = tk.StringVar(master=self.root)
        self.analyze_var = tk.StringVar(master=self.root)
        self.preview_var = tk.StringVar(master=self.root, value="48")

        self.editor_document: PA9EditorDocument | None = None
        self._editor_status_cache = None
        self.editor_vars: dict[str, Any] = {}
        self._editor_traces: list[tuple[Any, str, str]] = []
        self.editor_input_widgets: list[Any] = []
        self.editor_party_widgets: list[Any] = []
        self.editor_calculated_widgets: list[Any] = []
        self.editor_helper_buttons: list[Any] = []
        self._editor_populating = False
        self.editor_form_dirty = False
        self.editor_field_widgets = {}
        self.editor_field_labels = {}
        self.editor_advanced_var = tk.BooleanVar(master=root, value=False)
        self.editor_heading_var = tk.StringVar(master=root)
        self.editor_hint_var = tk.StringVar(master=root)
        self.editor_auto_var = tk.BooleanVar(master=self.root, value=False)
        self.editor_path_var = tk.StringVar(master=self.root, value=tr('新規作成、またはPA9を開いてください'))
        self.editor_status_var = tk.StringVar(master=self.root, value=tr('未読込'))
        self.editor_notice_var = tk.StringVar(
            master=self.root, value=tr('構造/checksumのみ検証します。ゲーム上の完全な合法性判定ではありません。')
        )
        self.editor_ev_total_var = tk.StringVar(master=self.root, value=tr('EV合計: —'))

        self.scroll_areas: list[_ScrollArea] = []
        self._flow_groups: list[tuple[Any, list[Any], int]] = []
        self._wrapped_labels: list[tuple[Any, Any]] = []
        self._editor_sections: list[tuple[Any, Any, Any, Any]] = []
        self._layout_pending = False
        self._scroll_resize_pending = False
        self._build_layout()
        self.notebook.select(self.editor_tab)
        self._main_tab_changed()
        root.bind("<MouseWheel>", self._mousewheel, add="+")
        root.bind("<Shift-MouseWheel>", self._mousewheel, add="+")
        if str(root.tk.call("tk", "windowingsystem")) == "x11":
            for event in ("<Button-4>", "<Button-5>",
                          "<Shift-Button-4>", "<Shift-Button-5>"):
                root.bind(event, self._mousewheel, add="+")
        root.bind("<FocusIn>", self._reveal_focus, add="+")
        for index in range(5):
            root.bind(f"<Alt-Key-{index + 1}>", lambda _e, i=index: self._task_shortcut(i))
        root.bind("<F1>", lambda _e: self._show_quickstart())
        root.bind("<Control-l>", lambda _e: self.language_combo.focus_set())
        root.bind("<Control-o>", lambda _e: self._editor_shortcut(self._editor_open))
        root.bind("<Control-n>", lambda _e: self._editor_shortcut(self._editor_new))
        root.bind("<Control-s>", lambda _e: self._editor_shortcut(self._editor_save_current))
        root.bind("<Control-Shift-S>", lambda _e: self._editor_shortcut(self._editor_save_as))
        self._update_backend_fields()
        if backend == "direct":
            self._refresh_radios()
        self._set_live_busy(False)
        root.after(self.POLL_MS, self._poll)

    def _configure_style(self) -> None:
        """Use stock ttk controls; keep OS font metrics and visible focus."""
        from tkinter import font
        style = self.ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        self._title_font = font.Font(root=self.root, font="TkDefaultFont")
        size = self._base_font_size = int(self._title_font.cget("size"))
        self._title_font.configure(size=size + 4 if size > 0 else size - 4, weight="bold")
        style.configure(".", background="#ffffff", foreground="#20314b")
        style.configure("App.TFrame", background="#edf2f7")
        style.configure("Title.TLabel", font=self._title_font, background="#edf2f7", foreground="#172b4d")
        style.configure("Hint.TLabel", foreground="#53647a")
        style.configure("Badge.TLabel", foreground="#815600")
        style.configure("TEntry", fieldbackground="#ffffff", bordercolor="#c4cfdd", padding=5)
        style.map("TEntry", bordercolor=[("invalid", "#b42318"), ("focus", "#2864c5")],
                  fieldbackground=[("disabled", "#f2f5f8"), ("readonly", "#f5f7fa")])
        style.configure("TCombobox", fieldbackground="#ffffff", padding=4, arrowsize=14)
        style.map("TCombobox", fieldbackground=[("readonly", "#ffffff"), ("disabled", "#f2f5f8")],
                  foreground=[("disabled", "#68788b"), ("readonly", "#20314b")])
        style.configure("TMenubutton", padding=(12, 6), background="#f8fafc", bordercolor="#c4cfdd")
        style.configure("TButton", background="#f8fafc", bordercolor="#c4cfdd", padding=(12, 6), width=0)
        style.map("TButton", background=[("active", "#e8eff9")], bordercolor=[("focus", "#2864c5")])
        style.configure("Accent.TButton", background="#245bb2", foreground="#ffffff", padding=(12, 6))
        style.map("Accent.TButton", background=[("disabled", "#e0e6ee"), ("active", "#19478f")],
                  foreground=[("disabled", "#63738a"), ("!disabled", "#ffffff")])
        style.configure("Stop.TButton", foreground="#a12933")
        style.configure("Danger.TCheckbutton", foreground="#a12933")
        style.configure("TLabelframe", bordercolor="#d6dfe9", relief="solid")
        style.configure("TLabelframe.Label", foreground="#415571")
        style.configure("TNotebook", borderwidth=0, tabmargins=(0, 3, 0, 0))
        style.configure("TNotebook.Tab", padding=(12, 7), background="#edf2f7")
        style.map("TNotebook.Tab", background=[("selected", "#ffffff")], foreground=[("selected", "#174a99")])
        # Keep the existing Notebook selection API; its task navigation lives
        # outside the scrolling content so a long connection form cannot hide it.
        style.layout("Workspace.TNotebook.Tab", [])
        style.configure("Workspace.TNotebook", borderwidth=0, tabmargins=0)
        style.layout("Nav.TRadiobutton", [("Button.border", {"sticky": "nswe", "children": [
            ("Button.focus", {"sticky": "nswe", "children": [
                ("Button.padding", {"sticky": "nswe", "children": [
                    ("Button.label", {"sticky": "nswe"})]})]})]})])
        style.configure("Nav.TRadiobutton", padding=(12, 8), background="#edf2f7", bordercolor="#d4deea",
                        foreground="#415571", anchor="center", focuscolor="#162c4f")
        style.map("Nav.TRadiobutton", background=[("selected", "#245bb2"), ("active", "#dde8f6")],
                  foreground=[("selected", "#ffffff")], bordercolor=[("focus", "#162c4f")])

    def _select_task(self, index: int) -> None:
        self.notebook.select(index)

    def _task_shortcut(self, index: int) -> str:
        if self.root.grab_current() is None and not self.close_when_idle:
            self._select_task(index)
        return "break"

    def _editor_shortcut(self, callback: Any) -> str:
        if self.root.grab_current() is None and not self.close_when_idle:
            self.notebook.select(self.editor_tab)
            callback()
        return "break"

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._log_lines = 0

    def _main_tab_changed(self, _event: Any = None) -> None:
        selected = self.notebook.select()
        index = self.notebook.index(selected)
        self.task_var.set(str(index))
        self.task_combo.current(index)
        if selected == str(self.editor_tab):
            self.body.frame.grid_remove()
            self.editor_workspace.grid()
        else:
            self.editor_workspace.grid_remove()
            self.body.frame.grid()
        if selected in (str(self.trade_tab), str(self.probe_tab)):
            self.connection_panel.pack(fill="x", before=self.notebook)
        else:
            self.connection_panel.pack_forget()
        self.trade_button.pack_forget()
        self.probe_button.pack_forget()
        self.editor_save_actions.pack_forget()
        if selected in (str(self.trade_tab), str(self.probe_tab)):
            self.task_action_bar.grid()
            (self.trade_button if selected == str(self.trade_tab) else self.probe_button).pack(fill="x")
        elif selected == str(self.editor_tab) and self.editor_document is not None:
            self.task_action_bar.grid()
            self.editor_save_actions.pack(fill="x")
        else:
            self.task_action_bar.grid_remove()
        if self._active_page != index:
            self.body.canvas.yview_moveto(0)
            self._active_page = index
        self._schedule_layout()

    def _language_selected(self, _event: Any = None) -> None:
        selected = next((key for key, value in LANGUAGE_NAMES.items()
                         if value == self.language_var.get()), self.language)
        self.change_language(selected, persist=True)

    def change_language(self, language: str, *, persist: bool = False) -> bool:
        """Rebuild only presentation while idle, retaining all pending edits.

        Never populate fields from the document here: unapplied input is newer
        than the document. No save, backend restart, key read or radio call.
        """
        validate_language(language)
        if language == self.language:
            return True
        diagnostic = getattr(self, "_radio_diagnostic_thread", None)
        if (self.controller.is_busy or self._pending_results() or self.close_when_idle
                or (diagnostic is not None and diagnostic.is_alive())
                or self.root.grab_current() is not None):
            self.language_var.set(LANGUAGE_NAMES[self.language])
            self.status_var.set(tr("言語の変更は処理の終了後に行ってください。"))
            return False
        previous = self.language
        main_tab = self.notebook.index(self.notebook.select())
        editor_tab = self.editor_notebook.index(self.editor_notebook.select())
        log = self.log_text.get("1.0", "end-1c")
        result = self.result_text.get("1.0", "end-1c")
        mode = next(key for key in ("自動", "はい", "いいえ")
                    if tr(key, language=previous) == self.single_interface_var.get())
        for name in ("help_window", "environment_window"):
            window = getattr(self, name, None)
            if window is not None and window.winfo_exists():
                window.destroy()
        self.language = language
        set_language(language)
        self.language_var.set(LANGUAGE_NAMES[language])
        self.single_interface_var.set(tr(mode))
        dirty = self.editor_form_dirty
        self._editor_populating = True
        try:
            for name in self.editor_vars:
                if name not in {"nature", "stat_nature", "gender"} and not field_catalog(name):
                    continue
                value = self.editor_vars[name].get()
                if value:
                    # Combobox presentation is "number: localized name"; the
                    # data model deliberately accepts only the numeric part.
                    try:
                        number = parse_editor_value(FIELD_SPECS[name], display_input_id(field_catalog(name), value)
                                                    if field_catalog(name) else value.split(":", 1)[0])
                    except (TypeError, ValueError):
                        continue  # Preserve invalid/unapplied input verbatim.
                    self.editor_vars[name].set(self._editor_format_value(name, number))
        finally:
            self._editor_populating = False
        self.editor_form_dirty = dirty
        self.outer.destroy()
        for name in ("scroll_areas", "_flow_groups", "_wrapped_labels", "_editor_sections",
                     "editor_input_widgets", "editor_party_widgets",
                     "editor_calculated_widgets", "editor_helper_buttons"):
            setattr(self, name, [])
        self.editor_field_widgets = {}
        self.editor_field_labels = {}
        self._build_layout()
        self.log_text.configure(state="normal")
        self.log_text.insert("1.0", log)
        self.log_text.configure(state="disabled")
        self.result_text.configure(state="normal")
        self.result_text.insert("1.0", result)
        self.result_text.configure(state="disabled")
        self.notebook.select(main_tab)
        self.editor_notebook.select(editor_tab)
        if self.editor_document is None:
            self.editor_path_var.set(tr("新規作成、またはPA9を開いてください"))
            self.editor_notice_var.set(tr("構造/checksumのみ検証します。ゲーム上の完全な合法性判定ではありません。"))
        elif self.editor_document.path is None:
            self.editor_path_var.set(tr("新規PA9（未保存）"))
        self._editor_update_status()
        self._editor_update_ev_total()
        self._update_backend_fields()
        self._set_live_busy(False)
        self._main_tab_changed()
        if persist:
            try:
                save_language(language)
            except (OSError, RuntimeError, ValueError, TypeError):
                self.status_var.set(tr("言語設定を保存できませんでした。今回の表示言語は変更されています。"))
        return True

    def _flow(self, parent: Any, widgets: list[Any], columns: int = 2) -> None:
        # Start narrow; reflow from viewport width, never from the previous
        # requested width (which would prevent shrinking a wide layout).
        self._flow_groups.append((parent, widgets, columns))
        for row, widget in enumerate(widgets):
            widget.grid(row=row, column=0, sticky="ew", padx=4, pady=4)
        parent.columnconfigure(0, weight=1)
        parent.bind("<Configure>", self._schedule_layout, add="+")

    def _wrap(self, parent: Any, **kwargs: Any) -> Any:
        label = self.ttk.Label(parent, wraplength=240, **kwargs)
        self._wrapped_labels.append((parent, label))
        parent.bind("<Configure>", self._schedule_layout, add="+")
        return label

    def _schedule_layout(self, _event: Any = None) -> None:
        if not self._layout_pending:
            self._layout_pending = True
            self.root.after_idle(self._reflow_layout)

    def _available_width(self, widget: Any) -> int:
        viewports = [area.canvas for area in self.scroll_areas if area.contains(widget)]
        if not viewports:
            return max(160, self.outer.winfo_width() - 24)
        right = min(canvas.winfo_rootx() + canvas.winfo_width() for canvas in viewports)
        available = right - widget.winfo_rootx() - 12
        if widget.winfo_width() > 1:
            available = min(available, widget.winfo_width() - 8)
        return max(160, available)

    def _reflow_layout(self) -> None:
        self._layout_pending = False
        header, brand, utility = self._header_widgets
        base = self._base_font_size
        title_size = base if self.root.winfo_width() < 800 else (base + 4 if base > 0 else base - 4)
        if int(self._title_font.cget("size")) != title_size:
            self._title_font.configure(size=title_size)
        inline = brand.winfo_reqwidth() + utility.winfo_reqwidth() + 16 <= max(1, self.outer.winfo_width() - 24)
        brand.grid_configure(row=0, column=0, columnspan=1 if inline else 2)
        utility.grid_configure(row=0 if inline else 1, column=1 if inline else 0,
                               columnspan=1 if inline else 2, sticky="e" if inline else "w")
        for parent, widgets, maximum in self._flow_groups:
            if parent is self.navigation:
                compact = len(widgets) * (max(w.winfo_reqwidth() for w in widgets) + 8) > self._available_width(parent)
                self._compact_navigation = compact
                if compact:
                    for w in widgets:w.grid_remove()
                    self.task_combo.grid(row=0, column=0, columnspan=5, sticky="ew", padx=4, pady=4)
                    continue
                self.task_combo.grid_remove()
                for w in widgets:w.grid()
            available = self._available_width(parent)
            cell = max(getattr(widget, "_flow_min_width", widget.winfo_reqwidth()) for widget in widgets) + 8
            columns = max(1, min(maximum, available // cell))
            for column in range(maximum):
                parent.columnconfigure(column, weight=1 if column < columns else 0,
                                       uniform="flow" if column < columns else "")
            for index, widget in enumerate(widgets):
                widget.grid_configure(row=index // columns, column=index % columns)
        self.ttk.Style(self.root).configure(
            "Danger.TCheckbutton", wraplength=max(120, self._available_width(self.trade_tab) - 32)
        )
        for parent, left, right, footer in self._editor_sections:
            # Base the outer card decision on each card's full intrinsic field
            # layout, not its previous one/two-column allocation. Otherwise
            # parent and child breakpoints can oscillate forever at mid widths.
            def intrinsic_width(card: Any) -> int:
                for container, fields, columns in self._flow_groups:
                    if container is card:
                        return columns * (max(getattr(field, "_flow_min_width", field.winfo_reqwidth())
                                              for field in fields) + 8) + 24
                return card.winfo_reqwidth()
            wide = intrinsic_width(left) + intrinsic_width(right) + 12 <= self._available_width(parent)
            parent.columnconfigure(1, weight=1 if wide else 0)
            left.grid_configure(row=0, column=0, padx=(0, 5) if wide else 0)
            right.grid_configure(row=0 if wide else 1, column=1 if wide else 0,
                                 padx=(5, 0) if wide else 0, pady=0 if wide else (8, 0))
            footer.grid_configure(row=1 if wide else 2, columnspan=2 if wide else 1)
        for parent, label in self._wrapped_labels:
            label.configure(wraplength=max(100, min(self._available_width(parent), parent.winfo_width() - 8)))
        # ttk otherwise requests the largest of ALL tabs, even a hidden editor.
        selected = self.root.nametowidget(self.notebook.select())
        self.notebook.configure(width=selected.winfo_reqwidth(),
                                height=selected.winfo_reqheight())
        self.body._resize()
        self._editor_layout_mode()
        if not self._scroll_resize_pending:
            self._scroll_resize_pending = True
            self.root.after_idle(self._sync_scroll_extents)

    def _sync_scroll_extents(self) -> None:
        self._scroll_resize_pending = False
        # Explicit canvas-window dimensions can suppress a Configure event when
        # a child's requested size shrinks. Refresh after Tk settles the grid.
        for area in self.scroll_areas:
            area._resize()

    def _reveal_focus(self, event: Any) -> None:
        for area in reversed(self.scroll_areas):
            area.see(event.widget)

    @staticmethod
    def _wheel_units(event: Any) -> int:
        if getattr(event, "num", None) in (4, 5):
            return -1 if event.num == 4 else 1
        try:
            delta = int(getattr(event, "delta", 0))
        except (TypeError, ValueError):
            return 0
        return (-(max(1, abs(delta) // 120)) if delta > 0
                else max(1, abs(delta) // 120) if delta < 0 else 0)

    def _mousewheel(self, event: Any) -> str | None:
        # Native text/combobox scrolling must not also scroll the whole form.
        if event.widget.winfo_class() in ("Text", "Listbox", "Treeview", "TCombobox"):
            return None
        units = self._wheel_units(event)
        if not units:
            return None
        axis = "x" if getattr(event, "state", 0) & 1 else "y"
        for area in reversed(self.scroll_areas):
            if area.contains(event.widget):
                view = getattr(area.canvas, axis + "view")
                before = view()
                getattr(area.canvas, axis + "view_scroll")(units * 24, "units")
                if view() != before:
                    return "break"
        return None

    def _build_layout(self) -> None:
        self._building_layout = True
        ttk = self.ttk
        self.outer = outer = ttk.Frame(self.root, padding=12, style="App.TFrame")
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        self.task_action_bar = ttk.Frame(outer, style="App.TFrame")
        self.task_action_bar.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.body = _ScrollArea(outer, tk=self.tk, ttk=ttk)
        self.scroll_areas.append(self.body)
        self.body.frame.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self.body.canvas.bind("<Configure>", self._schedule_layout, add="+")
        content = self.body.content
        header = ttk.Frame(outer, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        brand = ttk.Label(header, text="za-ldn-trade", style="Title.TLabel")
        utility = ttk.Frame(header, style="App.TFrame")
        self.header_help_button = ttk.Button(utility, text=tr('ヘルプ'), command=self._show_quickstart)
        self.header_help_button.pack(side="right", padx=(8, 0))
        self.language_combo = ttk.Combobox(utility, textvariable=self.language_var,
            values=tuple(LANGUAGE_NAMES.values()), state="readonly", width=10)
        self.language_combo.pack(side="right")
        self.language_combo.bind("<<ComboboxSelected>>", self._language_selected)
        self._header_widgets = (header, brand, utility)
        header.columnconfigure(0, weight=1)
        brand.grid(row=0, column=0, sticky="w")
        utility.grid(row=0, column=1, sticky="e")
        navigation = self.navigation = ttk.Frame(outer, style="App.TFrame")
        navigation.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        self.task_buttons = []
        for index, title in enumerate((tr('交換'), tr('PA9編集'), tr('キャプチャ'), tr('解析'), tr('ログ'))):
            button = ttk.Radiobutton(navigation, text=title, variable=self.task_var,
                value=str(index), style="Nav.TRadiobutton", command=lambda i=index: self._select_task(i))
            self.task_buttons.append(button)
        self.task_combo = ttk.Combobox(navigation, state="readonly", width=18,
            values=(tr('交換'), tr('PA9編集'), tr('キャプチャ'), tr('解析'), tr('ログ')))
        self.task_combo.bind("<<ComboboxSelected>>", lambda _e: self._select_task(self.task_combo.current()))
        self._flow(navigation, self.task_buttons, columns=5)

        self.connection_panel = connection = ttk.LabelFrame(content, text=tr('接続'), padding=10)
        connection.pack(fill="x")
        self._wrap(connection, text=tr("無線機能は実験的です"), style="Badge.TLabel").pack(fill="x", pady=(0, 6))
        methods = ttk.Frame(connection)
        methods.pack(fill="x")
        self.direct_radio = ttk.Radiobutton(
            methods, text=tr('このPCのWi-Fi（Linux）'), variable=self.backend_var, value="direct",
            command=self._backend_selected,
        )
        self.relay_radio = ttk.Radiobutton(
            methods, text=tr('準備済みの接続を使う'), variable=self.backend_var, value="relay",
            command=self._backend_selected,
        )
        self._flow(methods, [self.direct_radio, self.relay_radio])
        if not sys.platform.startswith("linux"):
            self.direct_radio.state(["disabled"])
        self.radio_panel = ttk.Frame(connection)
        ttk.Label(self.radio_panel, text=tr('使うWi-Fiアダプター')).pack(anchor="w")
        chooser = ttk.Frame(self.radio_panel)
        chooser.pack(fill="x")
        self.radio_refresh_button = ttk.Button(chooser, text=tr('一覧を更新'), command=self._refresh_radios)
        self.radio_refresh_button.pack(side="right", padx=(6, 0))
        self.radio_combo = ttk.Combobox(chooser, textvariable=self.radio_var, state="readonly", width=18)
        self.radio_combo.pack(side="left", fill="x", expand=True)
        self.radio_combo.bind("<<ComboboxSelected>>", self._radio_selected)
        self.radio_notice = self._wrap(self.radio_panel, style="Hint.TLabel")
        self.radio_notice.pack(fill="x", pady=(4, 0))
        fields = ttk.Frame(connection)
        fields.pack(fill="x")
        groups = {}

        def field(key: str, label: str, *, wrap: bool = False) -> Any:
            group = ttk.Frame(fields)
            if wrap:
                self._wrap(group, text=label).pack(fill="x")
            else:
                ttk.Label(group, text=label).pack(anchor="w")
            row = ttk.Frame(group)
            row.pack(fill="x")
            groups[key] = group
            return row

        self.nickname_entry = ttk.Entry(field("nickname", tr("表示名")), textvariable=self.nickname_var, width=18)
        self.nickname_entry.pack(fill="x")
        keys = field("keys", tr('鍵ファイル（prod.keys）'))
        self.keys_button = ttk.Button(keys, text=tr('参照'), width=0, command=self._browse_keys)
        self.keys_button.pack(side="right", padx=(4, 0))
        self.keys_entry = ttk.Entry(keys, textvariable=self.keys_var, width=16)
        self.keys_entry.pack(side="left", fill="x", expand=True)
        self.link_code_entry = ttk.Entry(
            field("code", tr("通信パスワード（8桁）")), textvariable=self.link_code_var, width=12, show="*"
        )
        self.link_code_entry.pack(fill="x")
        self.title_version_combo = ttk.Combobox(
            field("version", tr("Z-Aの版")), textvariable=self.title_version_var,
            values=SUPPORTED_ZA_TITLE_VERSIONS, state="readonly", width=10,
        )
        self.title_version_combo.pack(fill="x")
        self.relay_entry = ttk.Entry(field("relay", tr('中継先アドレス（Relay）')), textvariable=self.relay_var, width=18)
        self.relay_entry.pack(fill="x")
        self.phy_entry = ttk.Entry(field("phy", tr('選択した無線機（PHY）')), textvariable=self.phy_var, width=18, state="readonly")
        self.phy_entry.pack(fill="x")
        self.interface_entry = ttk.Entry(field("interface", tr('一時インターフェイス名')), textvariable=self.interface_var, width=18)
        self.interface_entry.pack(fill="x")
        self.single_interface_combo = ttk.Combobox(
            field("reuse", tr('既存Wi-Fiを再利用')), textvariable=self.single_interface_var,
            values=(tr('自動'), tr('はい'), tr('いいえ')), state="readonly", width=10,
        )
        self.single_interface_combo.pack(fill="x")
        self.single_interface_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_backend_fields())
        helper = field("bss_helper", tr("AWUS036ACH接続補助（任意）"), wrap=True)
        self.bss_helper_clear = ttk.Button(helper, text=tr("解除"), width=0, command=self._clear_bss_helper)
        self.bss_helper_clear.pack(side="right", padx=(4, 0))
        self.bss_helper_browse = ttk.Button(helper, text=tr("参照"), width=0, command=self._browse_bss_helper)
        self.bss_helper_browse.pack(side="right", padx=(4, 0))
        self.bss_helper_entry = ttk.Entry(helper, textvariable=self.bss_helper_var, width=1, state="readonly")
        self.bss_helper_entry.pack(side="left", fill="x", expand=True)
        self.connection_fields = fields
        self.connection_groups = groups
        self._flow(fields, list(groups.values()))
        self.connection_advanced_check = ttk.Checkbutton(connection, text=tr('詳細設定'),
            variable=self.connection_advanced_var, command=self._update_backend_fields)
        self.connection_advanced_check.pack(anchor="w", pady=(4, 0))
        self.connection_hint = self._wrap(connection, style="Hint.TLabel")
        self.connection_hint.pack(fill="x", pady=(6, 0))
        connection_actions = ttk.Frame(connection)
        connection_actions.pack(fill="x", pady=(4, 0))
        self.check_button = ttk.Button(
            connection_actions, text=tr('環境チェック'), width=0, command=self._check_environment
        )
        self.help_button = self.header_help_button
        self.check_button.pack(anchor="e")

        self.notebook = notebook = ttk.Notebook(content, width=1, height=1, style="Workspace.TNotebook")
        notebook.pack(fill="both", expand=True, pady=(10, 8))
        trade_tab = ttk.Frame(notebook, padding=10)
        self.trade_tab = trade_tab
        self.editor_tab = editor_tab = ttk.Frame(notebook, padding=10)
        self.probe_tab = probe_tab = ttk.Frame(notebook, padding=10)
        inspect_tab = ttk.Frame(notebook, padding=10)
        log_tab = ttk.Frame(notebook, padding=8)
        for tab, title in ((trade_tab, tr('交換')), (editor_tab, tr('PA9編集')),
                           (probe_tab, tr('キャプチャ')), (inspect_tab, tr('解析')), (log_tab, tr('ログ'))):
            notebook.add(tab, text=title)
            tab.bind("<Configure>", self._schedule_layout, add="+")
        notebook.bind("<<NotebookTabChanged>>", self._main_tab_changed, add="+")
        notebook.enable_traversal()
        self._build_trade_tab(trade_tab)
        # The editor owns one form viewport. Its toolbar and section navigation
        # must not disappear inside the connection/task scroller.
        self.editor_workspace = ttk.Frame(outer, padding=(10, 8))
        self.editor_workspace.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self.editor_workspace.bind("<Configure>", self._schedule_layout, add="+")
        self._compact_editor = None
        self._build_editor_tab(self.editor_workspace)
        self._build_probe_tab(probe_tab)
        self._build_inspect_tab(inspect_tab)
        self.log_text = self.scrolledtext.ScrolledText(
            log_tab, wrap="word", width=1, height=12, state="disabled", font="TkFixedFont",
            background="#ffffff", foreground="#20314b", relief="flat", borderwidth=0, padx=10, pady=8
        )
        self.log_text.pack(fill="both", expand=True)
        self.clear_log_button = ttk.Button(log_tab, text=tr("ログを消去"), command=self._clear_log)
        self.clear_log_button.pack(anchor="e", pady=(6, 0))

        # Outside the scroller: long status text must never push these offscreen.
        status = ttk.Frame(outer)
        status.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        status.columnconfigure(1, weight=1)
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=60)
        self.progress.grid(row=0, column=0, padx=(0, 8))
        self.status_label = ttk.Label(status, textvariable=self.status_var, width=1)
        self.status_label.grid(row=0, column=1, sticky="ew")
        self.stop_button = ttk.Button(status, text=tr('停止'), width=0, style='Stop.TButton', command=self._stop)
        self.stop_button.grid(row=0, column=2, padx=(8, 0))
        self.exit_button = ttk.Button(status, text=tr('終了'), width=0, command=self._request_close)
        self.exit_button.grid(row=0, column=3, padx=(8, 0))
        self._building_layout = False
        self._main_tab_changed()
        self._schedule_layout()

    def _editor_var(self, name: str, *, boolean: bool = False) -> Any:
        variable = self.editor_vars.get(name)
        if variable is not None:
            return variable
        variable = self.tk.BooleanVar(master=self.root, value=False) if boolean else self.tk.StringVar(master=self.root)
        callback = variable.trace_add(
            "write", lambda *_args, field=name: self._editor_variable_changed(field)
        )
        self._editor_traces.append((variable, "write", callback))
        self.editor_vars[name] = variable
        return variable

    def _editor_entry(
        self,
        parent: Any,
        name: str,
        *,
        width: int = 15,
        party_only: bool = False,
    ) -> Any:
        widget = self.ttk.Entry(
            parent, textvariable=self._editor_var(name), width=width
        )
        self.editor_field_widgets[name] = widget
        self.editor_input_widgets.append(widget)
        if party_only:
            self.editor_party_widgets.append(widget)
        return widget

    def _editor_labeled_entry(
        self,
        parent: Any,
        row: int,
        column: int,
        name: str,
        label: str,
        *,
        width: int = 15,
        party_only: bool = False,
    ) -> Any:
        self.editor_field_labels[name] = label
        group = self.ttk.Frame(parent)
        group.grid(row=row, column=column, sticky="ew", padx=4, pady=4)
        group.columnconfigure(0, weight=1)
        self._wrap(group, text=label, style="Hint.TLabel").grid(row=0, column=0, sticky="ew", pady=(0, 3))
        widget = self._editor_entry(group, name, width=width, party_only=party_only)
        widget.grid(row=1, column=0, sticky="ew")
        group._flow_min_width = widget.winfo_reqwidth()
        if field_catalog(name):
            button = self.ttk.Button(group, text=tr('選ぶ'), width=0,
                                     command=lambda: self._editor_pick_catalog(name))
            button.grid(row=1, column=1, padx=(4, 0))
            self.editor_helper_buttons.append(button)
            group._flow_min_width += button.winfo_reqwidth() + 4
        return widget

    def _build_editor_tab(self, tab: Any) -> None:
        ttk = self.ttk
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)
        toolbar = self.editor_toolbar = ttk.Frame(tab)
        toolbar.grid(row=0, column=0, sticky="ew")
        self.editor_new_button = ttk.Button(toolbar, width=0, text=tr('新規作成'), style="Accent.TButton", command=self._editor_new)
        self.editor_open_button = ttk.Button(toolbar, width=0, text=tr('開く'), command=self._editor_open)
        self.editor_more_button = ttk.Menubutton(toolbar, text=tr('操作'), width=0)
        self.editor_menu = self.tk.Menu(tab, tearoff=False)
        for text, command in ((tr('適用'), self._editor_apply), (tr('構造チェック'), self._editor_validate),
                              (tr('再読込'), self._editor_reload), (tr('変更を戻す'), self._editor_reset)):
            self.editor_menu.add_command(label=text, command=command)
        self.editor_menu.add_separator()
        self.editor_menu.add_checkbutton(label=tr('詳細・内部値を表示'), variable=self.editor_advanced_var,
                                         command=self._editor_toggle_details)
        self.editor_more_button.configure(menu=self.editor_menu)
        self._flow(toolbar, [self.editor_new_button, self.editor_open_button, self.editor_more_button], columns=3)
        compact = self.editor_compact_bar = ttk.Frame(tab)
        compact.columnconfigure(0, weight=1)
        self.editor_page_combo = ttk.Combobox(compact, state="readonly", width=12,
            values=(tr('基本'), tr('技'), tr('能力'), tr('親・出会い')))
        self.editor_page_combo.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.editor_page_combo.bind("<<ComboboxSelected>>",
            lambda _e: self.editor_notebook.select(self.editor_page_combo.current()))
        self.editor_file_button = ttk.Menubutton(compact, text=tr('ファイル'), width=0)
        self.editor_file_button.grid(row=0, column=1, padx=(0, 6))
        self.editor_file_menu = self.tk.Menu(self.editor_file_button, tearoff=False)
        for text, command in ((tr('新規作成'), self._editor_new), (tr('開く'), self._editor_open)):
            self.editor_file_menu.add_command(label=text, command=command)
        self.editor_file_menu.add_separator()
        self.editor_file_menu.add_cascade(label=tr('操作'), menu=self.editor_menu)
        self.editor_file_button.configure(menu=self.editor_file_menu)
        self.editor_info_button = ttk.Button(compact, text=tr('保存の注意'), width=0,
            command=self._editor_show_info)
        self.editor_info_button.grid(row=0, column=2)

        actions = self.editor_actions = ttk.Frame(tab)
        actions.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.editor_reload_button = ttk.Button(actions, width=0, text=tr('再読込'), command=self._editor_reload)
        self.editor_save_actions = self.ttk.Frame(self.task_action_bar)
        self.editor_save_as_button = ttk.Button(self.editor_save_actions, width=0, text=tr('名前を付けて保存'), command=self._editor_save_as)
        self.editor_save_button = ttk.Button(self.editor_save_actions, width=0, text=tr('保存'), style="Accent.TButton", command=self._editor_save_current)
        self._flow(self.editor_save_actions, [self.editor_save_button, self.editor_save_as_button], columns=2)
        self.editor_apply_button = ttk.Button(actions, width=0, text=tr('適用'), command=self._editor_apply)
        self.editor_validate_button = ttk.Button(actions, width=0, text=tr('構造チェック'), command=self._editor_validate)
        self.editor_reset_button = ttk.Button(actions, width=0, text=tr('変更を戻す'), command=self._editor_reset)
        self._flow(actions, [self.editor_reload_button, self.editor_apply_button, self.editor_validate_button, self.editor_reset_button], columns=4)

        summary = self.editor_summary = ttk.Frame(tab)
        summary.grid(row=2, column=0, sticky="ew", pady=(8, 6))
        path_row = self.editor_path_row = ttk.Frame(summary)
        path_row.pack(fill="x", pady=(0, 5))
        ttk.Label(path_row, text="PA9", style="Hint.TLabel").pack(side="left", padx=(0, 8))
        self.editor_path_entry = ttk.Entry(path_row, textvariable=self.editor_path_var, state="readonly", width=1)
        self.editor_path_entry.pack(side="left", fill="x", expand=True)
        self.editor_heading_label = self._wrap(summary, textvariable=self.editor_heading_var)
        self.editor_heading_label.pack(fill="x")
        self.editor_status_label = self._wrap(summary, textvariable=self.editor_status_var, style="Hint.TLabel")

        self.editor_notebook = editor_tabs = ttk.Notebook(tab)
        editor_tabs.grid(row=3, column=0, sticky="nsew")
        editor_tabs.bind("<<NotebookTabChanged>>", self._editor_page_changed)
        editor_tabs.enable_traversal()
        basic = self._editor_scroll_page(editor_tabs, tr('基本'))
        moves = self._editor_scroll_page(editor_tabs, tr('技'))
        stats = self._editor_scroll_page(editor_tabs, tr('能力'))
        ot = self._editor_scroll_page(editor_tabs, tr('親・出会い'))
        self._build_editor_basic(basic)
        self._build_editor_moves(moves)
        self._build_editor_stats(stats)
        self._build_editor_ot(ot)
        self.editor_notice_label = self._wrap(tab, textvariable=self.editor_hint_var, style="Hint.TLabel")
        self.editor_notice_label.grid(row=4, column=0, sticky="ew", pady=(8, 4))
        self.editor_empty = ttk.Frame(tab, padding=(20, 24))
        self.editor_empty.grid(row=3, column=0, sticky="nsew")
        self._wrap(self.editor_empty, text=tr('PA9を作成・編集する')).pack(fill="x", pady=(0, 8))
        self.editor_empty_hint = self._wrap(self.editor_empty,
            text=tr('「新規作成」で個体を作るか、「開く」で既存のPA9を選んでください。'),
            style="Hint.TLabel")
        self.editor_empty_hint.pack(fill="x")
        self.editor_empty_offline = self._wrap(self.editor_empty, text=tr('オフラインで利用できます。鍵やWi-Fiの設定は不要です。'),
            style="Hint.TLabel")
        self.editor_empty_offline.pack(fill="x", pady=(8, 0))
        empty_actions = ttk.Frame(self.editor_empty)
        empty_actions.pack(fill="x", pady=(12, 0))
        self.editor_empty_new_button = ttk.Button(empty_actions, text=tr('新規作成'),
            style="Accent.TButton", command=self._editor_new)
        self.editor_empty_open_button = ttk.Button(empty_actions, text=tr('開く'), command=self._editor_open)
        self._flow(empty_actions, [self.editor_empty_new_button, self.editor_empty_open_button], columns=2)
        self._editor_toggle_details()
        self._editor_update_controls()

    def _editor_scroll_page(self, notebook: Any, title: str) -> Any:
        """Create a vertically scrollable editor page for small displays."""

        area = _ScrollArea(notebook, tk=self.tk, ttk=self.ttk)
        self.scroll_areas.append(area)
        notebook.add(area.frame, text=title)
        return area.content

    def _build_editor_basic(self, tab: Any) -> None:
        ttk = self.ttk
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        identity = ttk.LabelFrame(tab, text=tr('個体'), padding=9)
        identity.grid(row=0, column=0, sticky="nsew")
        groups = []
        for index, (name, label) in enumerate((("species_internal", tr('種族')),
                ("nickname", tr('ニックネーム')), ("stat_level", tr('レベル')),
                ("held_item", tr('持ち物')), ("ability", tr('特性')))):
            entry = self._editor_labeled_entry(identity, index, 0, name, label,
                                               width=13, party_only=name == "stat_level")
            groups.append(entry.master)
        self._flow(identity, groups, columns=2)
        traits = ttk.LabelFrame(tab, text=tr('性格・性別'), padding=9)
        traits.grid(row=0, column=1, sticky="nsew")
        groups = []
        options = {
            "nature": tuple(f"{i}: {tr(n)}" for i, n in enumerate(NATURE_NAMES_JA)),
            "stat_nature": tuple(f"{i}: {tr(n)}" for i, n in enumerate(NATURE_NAMES_JA)),
            "gender": tuple(f"{i}: {tr(n)}" for i, n in enumerate(GENDER_NAMES_JA)),
        }
        for name, label in (("nature", tr('性格')), ("stat_nature", tr('補正性格 (Mint)')), ("gender", tr('性別'))):
            group = ttk.Frame(traits)
            self._wrap(group, text=label, style="Hint.TLabel").pack(fill="x", pady=(0, 3))
            combo = ttk.Combobox(group, textvariable=self._editor_var(name), values=options[name], state="readonly", width=18)
            combo.pack(fill="x")
            group._flow_min_width = combo.winfo_reqwidth()
            self.editor_input_widgets.append(combo)
            self.editor_field_widgets[name] = combo
            self.editor_field_labels[name] = label
            groups.append(group)
        self._flow(traits, groups, columns=1)
        details = ttk.Frame(tab)
        details.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self._editor_sections.append((tab, identity, traits, details))
        alpha = ttk.Checkbutton(details, text=tr('オヤブン'), variable=self._editor_var("is_alpha", boolean=True))
        alpha.pack(anchor="w")
        self.editor_input_widgets.append(alpha)
        self.editor_advanced_check = ttk.Checkbutton(details, text=tr('詳細・内部値を表示'),
            variable=self.editor_advanced_var, command=self._editor_toggle_details)
        self.editor_advanced_check.pack(anchor="w", pady=(6, 0))
        technical = self.editor_technical_frame = ttk.LabelFrame(details, text=tr('詳細・内部値'), padding=8)
        groups = []
        for index, (name, label) in enumerate((("form", tr('フォルム')), ("experience", tr('経験値')),
                ("ability_number", tr('特性スロット')), ("tid", "TID16"), ("sid", "SID16"),
                ("pid", "PID"), ("ec", "EC"))):
            entry = self._editor_labeled_entry(technical, index, 0, name, label, width=12)
            groups.append(entry.master)
        for name, label in (("fateful_encounter", tr('運命的な出会い')), ("is_egg", tr('タマゴ')),
                            ("is_nicknamed", tr('ニックネーム設定済み'))):
            check = ttk.Checkbutton(technical, text=label, variable=self._editor_var(name, boolean=True))
            self.editor_input_widgets.append(check)
            groups.append(check)
        self._flow(technical, groups, columns=2)


    def _build_editor_moves(self, tab: Any) -> None:
        tab.columnconfigure(0, weight=1)
        for slot in range(1, 5):
            card = self.ttk.LabelFrame(tab, text=tr('技 {v0}', v0=slot), padding=8)
            card.grid(row=slot-1, column=0, sticky="ew", pady=(0, 8))
            groups = []
            for row, name, label in ((0, f"move{slot}", tr('技')),
                                     (1, f"relearn_move{slot}", tr('思い出し技'))):
                entry = self._editor_labeled_entry(card, row, 0, name, label, width=18)
                groups.append(entry.master)
            pp = self.ttk.Frame(card)
            for col, name, label in ((0, f"move{slot}_pp", "PP"), (1, f"move{slot}_pp_ups", "PP Up (0–3)")):
                self.ttk.Label(pp, text=label, style="Hint.TLabel").grid(row=0, column=col, sticky="w", padx=4)
                entry = self._editor_entry(pp, name, width=6)
                entry.grid(row=1, column=col, sticky="ew", padx=4)
                pp.columnconfigure(col, weight=1)
            pp._flow_min_width = 2 * entry.winfo_reqwidth() + 16
            groups.append(pp)
            self._flow(card, groups, columns=2)


    def _build_editor_stats(self, tab: Any) -> None:
        ttk = self.ttk
        tab.columnconfigure(0, weight=1)
        calculation = ttk.Frame(tab)
        calculation.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.editor_auto_check = ttk.Checkbutton(calculation, text=tr('能力値を自動計算'),
            variable=self.editor_auto_var, command=self._editor_set_auto)
        self.editor_recalculate_button = ttk.Button(calculation, text=tr('再計算'), command=self._editor_recalculate)
        self._flow(calculation, [self.editor_auto_check, self.editor_recalculate_button])
        training = ttk.LabelFrame(tab, text=tr('個体値・努力値'), padding=8)
        training.grid(row=1, column=0, sticky="ew")
        training.columnconfigure(1, weight=1)
        training.columnconfigure(2, weight=1)
        ttk.Label(training, text=tr('能力')).grid(row=0, column=0, sticky="w")
        ttk.Label(training, text="IV (0–31)").grid(row=0, column=1, sticky="w")
        ttk.Label(training, text="EV (0–252)").grid(row=0, column=2, sticky="w")
        stat_rows = (
            ("hp", "HP"),
            ("atk", tr('こうげき')),
            ("def", tr('ぼうぎょ')),
            ("spa", tr('とくこう')),
            ("spd", tr('とくぼう')),
            ("spe", tr('すばやさ')),
        )
        for row, (suffix, label) in enumerate(stat_rows, start=1):
            ttk.Label(training, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 10), pady=2
            )
            iv = self._editor_entry(training, f"iv_{suffix}", width=10)
            iv.grid(row=row, column=1, sticky="ew", padx=(0, 10), pady=2)
            ev = self._editor_entry(training, f"ev_{suffix}", width=10)
            ev.grid(row=row, column=2, sticky="ew", pady=2)
        helpers = ttk.Frame(training)
        helpers.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(7, 0))
        ev_total = ttk.Label(helpers, textvariable=self.editor_ev_total_var)
        iv_all_button = ttk.Button(
            helpers, text=tr('IVをすべて31'), command=lambda: self._editor_set_fields("iv_", "31")
        )
        ev_zero_button = ttk.Button(
            helpers, text=tr('EVをすべて0'), command=lambda: self._editor_set_fields("ev_", "0")
        )
        self._flow(helpers, [ev_total, iv_all_button, ev_zero_button], columns=3)
        self.editor_helper_buttons.extend((iv_all_button, ev_zero_button))

        party = ttk.LabelFrame(tab, text=tr('Partyキャッシュ (0x158のみ)'), padding=8)
        party.grid(row=2, column=0, sticky="ew", pady=(9, 0))
        party_groups = []
        party_fields = (
            ("stat_hp_current", tr('現在HP')),
            ("stat_hp_max", tr('最大HP')),
            ("stat_atk", tr('こうげき')),
            ("stat_def", tr('ぼうぎょ')),
            ("stat_spa", tr('とくこう')),
            ("stat_spd", tr('とくぼう')),
            ("stat_spe", tr('すばやさ')),
        )
        for index, (name, label) in enumerate(party_fields):
            widget = self._editor_labeled_entry(
                party,
                index // 4,
                index % 4,
                name,
                label,
                width=9,
                party_only=name != "stat_hp_current",
            )
            party_groups.append(widget.master)
            if name != "stat_level":
                self.editor_calculated_widgets.append(widget)
        self._flow(party, party_groups, columns=4)
        self._wrap(
            tab,
            text=tr('自動計算ONなら適用・保存で更新。Lv変更は成長曲線から経験値へ反映。再計算は全回復も行います。'),
            style="Hint.TLabel",
        ).grid(row=3, column=0, sticky="ew", pady=(7, 0))

    def _build_editor_ot(self, tab: Any) -> None:
        groups = []
        fields = (
            ("original_trainer_name", tr('親名')),
            ("language", tr('言語ID')),
            ("version", tr('出身version ID')),
            ("ot_friendship", tr('なつき度')),
            ("ot_gender", tr('親の性別 (0/1)')),
            ("met_level", tr('出会いLv')),
            ("obedience_level", tr('従うLv')),
            ("ball", tr('ボールID')),
            ("met_location", tr('出会い場所ID')),
            ("egg_location", tr('タマゴ場所ID')),
            ("met_year", tr('出会い年 (2000+)')),
            ("met_month", tr('出会い月')),
            ("met_day", tr('出会い日')),
        )
        for index, (name, label) in enumerate(fields):
            entry = self._editor_labeled_entry(tab, index // 2, index % 2, name, label, width=16)
            groups.append(entry.master)
        self._flow(tab, groups, columns=2)
        # The field label already identifies the year offset; detailed format
        # explanations live in the PA9 guide instead of widening this form.

    def _editor_page_changed(self, _event=None) -> None:
        selected = self.editor_notebook.select()
        if selected:
            self.editor_page_combo.current(self.editor_notebook.index(selected))

    def _editor_show_info(self) -> None:
        self.messagebox.showinfo(tr('保存と構造チェック'), self.editor_notice_var.get(), parent=self.root)

    def _editor_layout_mode(self) -> None:
        if self._building_layout or not self.editor_workspace.winfo_ismapped():
            return
        from tkinter import font
        text_font = font.nametofont("TkDefaultFont", root=self.root)
        tabs_width = sum(text_font.measure(self.editor_notebook.tab(t, "text")) + 32
                         for t in self.editor_notebook.tabs())
        compact = (self.editor_workspace.winfo_height() < text_font.metrics("linespace") * 17
                   or self.editor_workspace.winfo_width() < tabs_width + 24)
        if compact:
            narrow = (self.editor_workspace.winfo_width() <
                      self.editor_file_button.winfo_reqwidth() + self.editor_info_button.winfo_reqwidth() + 180)
            bar = self.editor_compact_bar
            self.editor_page_combo.grid_configure(row=0, column=0, columnspan=2 if narrow else 1)
            self.editor_file_button.grid_configure(row=1 if narrow else 0, column=0 if narrow else 1,
                                                  sticky="ew" if narrow else "")
            self.editor_info_button.grid_configure(row=1 if narrow else 0, column=1 if narrow else 2,
                                                  sticky="ew" if narrow else "")
            bar.columnconfigure(1, weight=1 if narrow else 0)
        if compact == self._compact_editor:
            return
        self._compact_editor = compact
        self.editor_notebook.configure(style="Workspace.TNotebook" if compact else "TNotebook")
        if compact:
            self.editor_toolbar.grid_remove()
            self.editor_compact_bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        else:
            self.editor_compact_bar.grid_remove()
            self.editor_toolbar.grid()
        self._editor_page_changed()
        self._editor_update_controls()

    def _editor_toggle_details(self) -> None:
        if hasattr(self, "editor_notice_label"):
            self.editor_notice_label.configure(textvariable=self.editor_notice_var if self.editor_advanced_var.get() else self.editor_hint_var)
        if self.editor_advanced_var.get():
            if self.editor_document is not None and not self._compact_editor:
                self.editor_actions.grid()
            else:
                self.editor_actions.grid_remove()
            self.editor_path_row.pack(fill="x", pady=(0, 5), before=self.editor_heading_label)
            self.editor_technical_frame.pack(fill="x", pady=(5, 0))
            self.editor_status_label.pack(fill="x", pady=(4, 0))
        else:
            self.editor_actions.grid_remove()
            self.editor_path_row.pack_forget()
            self.editor_technical_frame.pack_forget()
            self.editor_status_label.pack_forget()
        self._schedule_layout()


    def _editor_focus_field(self, name: str) -> None:
        widget = self.editor_field_widgets.get(name)
        if widget is None:
            return
        self.notebook.select(self.editor_tab)
        self._main_tab_changed()
        if str(widget).startswith(str(self.editor_technical_frame) + "."):
            self.editor_advanced_var.set(True)
            self._editor_toggle_details()
        for page in self.editor_notebook.tabs():
            if str(widget).startswith(page + "."):
                self.editor_notebook.select(page)
                break
        self.root.update_idletasks()
        for area in reversed(self.scroll_areas):
            area.see(widget)
        widget.state(["invalid"])
        widget.focus_set()


    def _editor_pick_catalog(self, name: str) -> None:
        if self.editor_document is None or self.close_when_idle or self.root.grab_current() is not None:
            return
        from .catalog_picker import CatalogPicker
        document = self.editor_document
        kind = field_catalog(name)
        try:
            initial_value = parse_editor_value(FIELD_SPECS[name],
                display_input_id(kind, self.editor_vars[name].get()))
            initial_form = (parse_editor_value(FIELD_SPECS["form"], self.editor_vars["form"].get())
                            if name == "species_internal" else None)
        except (TypeError, ValueError, KeyError, OSError):
            initial_value = initial_form = None
        def accept(choice):
            if self.editor_document is not document or self.close_when_idle:
                return False
            values = {name: self._editor_format_value(name, choice.value)}
            if name == "species_internal":
                values["form"] = str(choice.form)
            if all(self.editor_vars[key].get() == value for key, value in values.items()):
                return True
            previous = self._editor_populating
            self._editor_populating = True
            try:
                for key, value in values.items():
                    self.editor_vars[key].set(value)
            finally:
                self._editor_populating = previous
            self._editor_variable_changed(name)
            return True
        try:
            self.catalog_dialog = CatalogPicker(self.root, tk=self.tk, ttk=self.ttk,
                kind=kind, title=tr('名前で選択'), accept=accept,
                initial_value=initial_value, initial_form=initial_form)
        except (OSError, ValueError, KeyError) as exc:
            self.messagebox.showerror(tr('名前一覧を読み込めません'), display_safe(exc, limit=2048), parent=self.root)


    def _editor_variable_changed(self, _field: str) -> None:
        if self._editor_populating or self.editor_document is None:
            return
        self.editor_form_dirty = True
        widget = self.editor_field_widgets.get(_field)
        if widget is not None:
            widget.state(["!invalid"])
        if _field.startswith("ev_"):
            self._editor_update_ev_total()
        self._editor_update_status()

    def _editor_set_fields(self, prefix: str, value: str) -> None:
        if self.editor_document is None:
            return
        was_populating = self._editor_populating
        changed = ev_changed = False
        self._editor_populating = True
        try:
            for name, variable in self.editor_vars.items():
                if name.startswith(prefix):
                    changed = True
                    ev_changed |= name.startswith("ev_")
                    variable.set(value)
        finally:
            self._editor_populating = was_populating
            # Refresh once, also after a partially failed batch. Pending edits
            # must remain dirty; this does not apply or save the document.
            if changed and not was_populating:
                self._editor_variable_changed("ev_" if ev_changed else prefix)

    def _editor_format_value(self, name: str, value: object | None) -> object:
        if value is None:
            return ""
        spec = FIELD_SPECS[name]
        if spec.kind == "bool":
            return bool(value)
        if name in {"nature", "stat_nature"}:
            numeric = int(value)
            if 0 <= numeric < len(NATURE_NAMES_JA):
                return f"{numeric}: {tr(NATURE_NAMES_JA[numeric])}"
        if name == "gender":
            numeric = int(value)
            if 0 <= numeric < len(GENDER_NAMES_JA):
                return f"{numeric}: {tr(GENDER_NAMES_JA[numeric])}"
        if field_catalog(name):
            return display_value(field_catalog(name), int(value))
        if spec.hexadecimal:
            width = 8 if spec.maximum == 0xFFFFFFFF else 4
            return f"0x{int(value):0{width}X}"
        return str(value)

    def _editor_populate(self) -> None:
        document = self.editor_document
        if document is None:
            return
        self._editor_populating = True
        try:
            for spec in EDITABLE_FIELDS:
                value = (
                    None
                    if spec.party_only and not document.record.is_party
                    else getattr(document.record, spec.name)
                )
                self.editor_vars[spec.name].set(
                    self._editor_format_value(spec.name, value)
                )
        finally:
            self._editor_populating = False
        self.editor_form_dirty = False
        self.editor_auto_var.set(document.auto_recalculate)
        self._editor_update_ev_total()
        self._editor_update_controls()
        self._editor_update_status()

    def _editor_update_ev_total(self) -> None:
        if self.editor_document is None:
            self.editor_ev_total_var.set(tr('EV合計: —'))
            return
        total = 0
        try:
            for name in ("ev_hp", "ev_atk", "ev_def", "ev_spa", "ev_spd", "ev_spe"):
                total += int(
                    parse_editor_value(
                        FIELD_SPECS[name], self.editor_vars[name].get()
                    )
                )
        except (TypeError, ValueError):
            self.editor_ev_total_var.set(tr('EV合計: 入力中'))
            return
        suffix = tr(' (通常上限510超過)') if total > 510 else ""
        self.editor_ev_total_var.set(tr('EV合計: {v0}{v1}', v0=total, v1=suffix))

    def _editor_is_dirty(self) -> bool:
        return bool(
            self.editor_document is not None
            and (self.editor_form_dirty or self.editor_document.dirty)
        )

    def _editor_update_controls(self) -> None:
        document = self.editor_document
        has_document = document is not None
        self.editor_notebook.grid() if has_document else self.editor_notebook.grid_remove()
        if not has_document:
            self.editor_toolbar.grid_remove()
            self.editor_compact_bar.grid_remove()
        elif self._compact_editor:
            self.editor_toolbar.grid_remove()
            self.editor_compact_bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        else:
            self.editor_compact_bar.grid_remove()
            self.editor_toolbar.grid()
        self.editor_empty.configure(padding=(12, 12) if self._compact_editor else (20, 24))
        self.editor_empty_hint.configure(text=tr('オフラインで作成・編集できます。') if self._compact_editor
            else tr('「新規作成」で個体を作るか、「開く」で既存のPA9を選んでください。'))
        if self._compact_editor:
            self.editor_empty_offline.pack_forget()
        else:
            self.editor_empty_offline.pack(fill="x", pady=(8, 0), after=self.editor_empty_hint)
        for group in (self.editor_summary, self.editor_notice_label):
            group.grid() if has_document and not self._compact_editor else group.grid_remove()
        self.editor_empty.grid_remove() if has_document else self.editor_empty.grid()
        self._editor_toggle_details()
        self.editor_new_button.configure(style="TButton" if has_document else "Accent.TButton")
        self.editor_save_as_button.configure(style="TButton")
        self.editor_save_button.configure(style="Accent.TButton")
        closing = self.close_when_idle
        active = document is not None and not closing
        self.editor_more_button.state(["!disabled"] if active else ["disabled"])
        self.editor_info_button.state(["!disabled"] if active else ["disabled"])
        self.editor_page_combo.state(["!disabled"] if active else ["disabled"])
        self.editor_file_button.state(["disabled"] if closing else ["!disabled"])
        self.editor_file_menu.entryconfigure(3, state="normal" if active else "disabled")
        self.editor_open_button.state(["disabled"] if closing else ["!disabled"])
        self.editor_new_button.state(["disabled"] if closing else ["!disabled"])
        self.editor_empty_new_button.state(["disabled"] if closing else ["!disabled"])
        self.editor_empty_open_button.state(["disabled"] if closing else ["!disabled"])
        self.editor_auto_check.state(["!disabled"] if active and document.record.is_party else ["disabled"])
        self.editor_recalculate_button.state(["!disabled"] if active else ["disabled"])
        for widget in self.editor_input_widgets:
            widget.state(["!disabled"] if active else ["disabled"])
        if active and not document.record.is_party:
            for widget in self.editor_party_widgets:
                widget.state(["disabled"])
        if active and document.auto_recalculate:
            for widget in self.editor_calculated_widgets:
                widget.state(["disabled"])
        button_state = ["!disabled"] if active else ["disabled"]
        for widget in (
            self.editor_reload_button,
            self.editor_validate_button,
            self.editor_reset_button,
            self.editor_apply_button,
            self.editor_save_button,
            self.editor_save_as_button,
            *self.editor_helper_buttons,
        ):
            widget.state(button_state)
        if active and document.path is None:
            self.editor_reload_button.state(["disabled"])
        if not self._building_layout:
            self._main_tab_changed()

    def _editor_update_status(self) -> None:
        document = self.editor_document
        dirty = self._editor_is_dirty()
        try:
            title = tr('PA9編集 *') if dirty else tr('PA9編集')
            self.notebook.tab(self.editor_tab, text=title)
            previous_title = self.task_buttons[1].cget("text")
            self.task_buttons[1].configure(text=title)
            self.task_combo.configure(values=tuple(b.cget("text") for b in self.task_buttons))
            self.task_combo.current(self.notebook.index(self.notebook.select()))
            if previous_title != title:
                self._schedule_layout()
        except Exception:
            pass
        if document is None:
            self._editor_status_cache = None
            self.editor_status_var.set(tr('未読込'))
            return
        # Pending text edits have not changed the PA9 yet. Cache presentation
        # validation for exactly these bytes, not mutable document identity.
        # A subclass may derive properties from other mutable state: do not
        # cache those validators merely because its raw PA9 bytes match.
        # Apply/save validation remains independent and is never skipped.
        signature = document.record.data
        cached = self._editor_status_cache
        if type(document.record) is not PA9 or cached is None or cached[0] != signature:
            issues = validate_editor_record(document.record)
            cached = (
                signature,
                sum(issue.severity == "error" for issue in issues),
                sum(issue.severity == "warning" for issue in issues),
                document.record.checksum_valid,
            )
            # Derived-property results must never seed a later plain PA9 cache.
            self._editor_status_cache = cached if type(document.record) is PA9 else None
        _, errors, warnings, checksum_valid = cached
        container = "Party 0x158" if document.record.is_party else "Stored 0x148"
        encryption = tr('暗号化') if document.input_encrypted else tr('復号')
        checksum = "OK" if checksum_valid else "INVALID"
        unsaved = tr(' | 未保存') if dirty else ""
        self.editor_status_var.set(
            tr('{v0} | 入力: {v1} | checksum: {v2} | 構造: error {v3}, warning {v4} | 合法性: 未検証{v5}', v0=container, v1=encryption, v2=checksum, v3=errors, v4=warnings, v5=unsaved)
        )
        name = display_value("species", document.record.species_internal)
        if document.path is not None:
            name = display_safe(document.path.name, limit=64) + " — " + name
        state = tr('未保存の変更あり') if dirty else tr('保存済み')
        if errors:
            state = tr('入力・構造を確認（{v0}件）', v0=errors)
        self.editor_heading_var.set(tr('{v0} · Lv{v1} · {v2}', v0=name,
            v1=document.record.stat_level if document.record.is_party else "—", v2=state))
        notice = (
            tr('自動計算ON: 種族・経験値・IV/EV・補正性格から能力値を更新し全回復します。')
            if document.auto_recalculate else tr('手動編集モード。能力値は「再計算」で更新・全回復できます。')
        )
        notice += tr(' 技・特性・出会い・RNG整合の修復や完全な合法性判定は行いません。')
        if document.derived_stats_may_be_stale:
            notice = tr('警告: 派生party能力値が古い可能性があります。') + notice
        self.editor_notice_var.set(notice)
        hint = (tr('保存時に能力値を再計算・全回復します。合法性の判定は行いません。')
                if document.auto_recalculate else tr('能力値は手動編集です。合法性の判定は行いません。'))
        if document.derived_stats_may_be_stale:
            hint = tr('能力値が未更新です。') + hint
        self.editor_hint_var.set(hint)

    def _editor_collect_changes(self) -> dict[str, object]:
        document = self.editor_document
        if document is None:
            raise ValueError(tr('PA9を先に開いてください'))
        changes: dict[str, object] = {}
        for spec in EDITABLE_FIELDS:
            if spec.party_only and not document.record.is_party:
                continue
            raw = self.editor_vars[spec.name].get()
            try:
                if field_catalog(spec.name):
                    raw = display_input_id(field_catalog(spec.name), raw)
                elif spec.name in {"nature", "stat_nature", "gender"}:
                    raw = str(raw).split(":", 1)[0]
                changes[spec.name] = parse_editor_value(spec, raw)
            except (TypeError, ValueError) as exc:
                self._editor_focus_field(spec.name)
                raise ValueError(tr('「{v0}」の入力を確認してください: {v1}',
                                    v0=self.editor_field_labels.get(spec.name, tr(spec.label)), v1=str(exc))) from exc
        return changes

    def _editor_apply_pending(self) -> bool:
        document = self.editor_document
        if document is None:
            return False
        try:
            document.apply(self._editor_collect_changes())
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.messagebox.showerror(
                tr('PA9編集エラー'), display_safe(exc, limit=2048), parent=self.root
            )
            return False
        self._editor_populate()
        return True

    def _editor_apply(self) -> None:
        if self._editor_apply_pending():
            if self._editor_is_dirty():
                self.status_var.set(tr('PA9編集をメモリ上へ適用しました (未保存)'))
            else:
                self.status_var.set(tr('PA9編集内容に変更はありません'))

    def _editor_validate(self) -> None:
        document = self.editor_document
        if document is None:
            return
        try:
            candidate = apply_pa9_changes(
                document.record, self._editor_collect_changes()
            )
            # ``apply_pa9_changes`` intentionally calculates a fresh checksum.
            # Preserve a checksum diagnostic from the currently loaded/applied
            # record so Structure Check cannot report an invalid source as
            # healthy merely because a future Apply/Save would repair it.
            checksum_issues = tuple(
                issue
                for issue in validate_editor_record(document.record)
                if issue.field == "checksum"
            )
            candidate_issues = tuple(
                issue
                for issue in validate_editor_record(candidate)
                if issue.field != "checksum"
            )
            issues = checksum_issues + candidate_issues
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.messagebox.showerror(
                tr('構造チェックエラー'), display_safe(exc, limit=2048), parent=self.root
            )
            return
        if issues:
            lines = "\n".join(
                f"- {issue.severity}: {issue.message}" for issue in issues[:12]
            )
            if len(issues) > 12:
                lines += tr('\n- …ほか{v0}件', v0=len(issues) - 12)
            self.messagebox.showwarning(
                tr('PA9構造チェック'),
                tr('{v0}\n\nゲーム上の完全な合法性は未検証です。', v0=lines),
                parent=self.root,
            )
        else:
            self.messagebox.showinfo(
                tr('PA9構造チェック'),
                tr('checksum・sanity・編集範囲は正常です。\nゲーム上の完全な合法性は未検証です。'),
                parent=self.root,
            )

    def _editor_load_path(self, path: str | Path) -> bool:
        try:
            document = PA9EditorDocument.load(path)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.messagebox.showerror(
                tr('PA9読込エラー'), display_safe(exc, limit=2048), parent=self.root
            )
            return False
        self.editor_document = document
        self.editor_path_var.set(str(document.path))
        self._editor_populate()
        self.status_var.set(tr('PA9を読み込みました'))
        return True

    def _editor_new(self) -> None:
        if self.close_when_idle:
            return
        previous = getattr(self, "editor_new_dialog", None)
        if previous is not None and previous.window.winfo_exists():
            previous.window.lift()
            return
        from .pa9_create_dialog import NewPA9Dialog
        self.editor_new_dialog = NewPA9Dialog(
            self.root, tk=self.tk, ttk=self.ttk, messagebox=self.messagebox,
            scroll_class=_ScrollArea, accept=self._editor_accept_new,
        )

    def _editor_accept_new(self, document: PA9EditorDocument) -> bool:
        if self.close_when_idle or not self._editor_confirm_leave():
            return False
        self.editor_document = document
        self.editor_path_var.set(tr('新規PA9（未保存）'))
        self._editor_populate()
        self.editor_notebook.select(0)
        self.root.after_idle(lambda: self._editor_reveal_new(document))
        self.status_var.set(tr('新規個体を作成しました。「保存」でファイルに書き出せます。'))
        return True

    def _editor_reveal_new(self, document: PA9EditorDocument) -> None:
        # Presentation only, after the creation modal is gone. Never steal
        # focus from another operation or from a user who has changed pages.
        if (self.close_when_idle or self.editor_document is not document
                or not self.root.winfo_exists() or self.root.grab_current() is not None
                or self.notebook.select() != str(self.editor_tab)
                or self.editor_notebook.index(self.editor_notebook.select()) != 0):
            return
        self.root.update_idletasks()
        widget = self.editor_field_widgets["nickname"]
        for area in reversed(self.scroll_areas):
            area.see(widget)
        widget.focus_set()
        widget.selection_range(0, "end")

    def _editor_set_auto(self) -> None:
        if self.editor_document is not None:
            self.editor_document.auto_recalculate = self.editor_auto_var.get()
            self._editor_update_controls()
            self._editor_update_status()

    def _editor_recalculate(self) -> None:
        if self.editor_document is None:
            return
        promote = not self.editor_document.record.is_party
        if promote and not self.messagebox.askyesno(
            tr('Party形式へ変換'), tr('Stored 328Bを能力値計算済みParty 344Bへ変換し、全回復しますか？'),
            default="no", parent=self.root,
        ):
            return
        if not self._editor_apply_pending():
            return
        try:
            self.editor_document.recalculate(promote=promote)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.messagebox.showerror(tr('能力値を計算できません'), display_safe(exc, limit=2048), parent=self.root)
            return
        self._editor_populate()
        self.status_var.set(tr('能力値を計算し、HP・状態を全回復しました（未保存）。合法性判定ではありません。'))

    def _editor_open(self) -> None:
        value = self.filedialog.askopenfilename(
            title=tr('編集するPA9を選択'),
            filetypes=[("PA9", "*.pa9 *.epa9 *.ea9"), ("All files", "*")],
            parent=self.root,
        )
        if not value:
            return
        if not self._editor_confirm_leave():
            return
        self._editor_load_path(value)

    def _editor_reload(self) -> None:
        if self.editor_document is None or self.editor_document.path is None:
            return
        if not self._editor_confirm_leave():
            return
        # Saving a document loaded through "All files" can require Save As,
        # which changes the document path.  Reload the successfully saved path,
        # not the stale path captured before the confirmation dialog.
        document = self.editor_document
        if document is not None and document.path is not None:
            self._editor_load_path(document.path)

    def _editor_reset(self) -> None:
        if self.editor_document is None or not self._editor_is_dirty():
            return
        if not self.messagebox.askyesno(
            tr('変更を戻す'),
            tr('最後に読み込み／保存した状態へ戻しますか？'),
            default="no",
            parent=self.root,
        ):
            return
        self.editor_document.reset()
        self._editor_populate()
        self.status_var.set(tr('PA9編集を元に戻しました'))

    def _editor_save_current(self, *, confirm: bool = True) -> bool:
        document = self.editor_document
        if document is None:
            return False
        if not self._editor_apply_pending():
            return False
        if document.path is None or document.path.suffix.lower() not in {
            ".pa9",
            ".epa9",
            ".ea9",
        }:
            return self._editor_save_as(apply_first=False)
        if confirm and not self.messagebox.askyesno(
            tr('上書き確認'),
            tr('読込元を原子的に置き換えますか？\n{v0}', v0=document.path),
            default="no",
            parent=self.root,
        ):
            return False
        try:
            output = document.save()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.messagebox.showerror(
                tr('PA9保存エラー'),
                tr('{v0}\n\n読込後に外部変更された場合は再読込するか、名前を付けて保存してください。', v0=display_safe(exc, limit=2048)),
                parent=self.root,
            )
            return False
        self.editor_path_var.set(str(output))
        self.editor_form_dirty = False
        self._editor_update_controls()
        self._editor_update_status()
        self.status_var.set(tr('PA9を保存しました: {v0}', v0=output))
        return True

    def _editor_save_as(self, *, apply_first: bool = True) -> bool:
        document = self.editor_document
        if document is None:
            return False
        if apply_first and not self._editor_apply_pending():
            return False
        encrypted = document.input_encrypted
        default_extension = ".epa9" if encrypted else ".pa9"
        value = self.filedialog.asksaveasfilename(
            title=tr('編集したPA9を保存（.pa9=復号 / .epa9・.ea9=暗号化）'),
            defaultextension=default_extension,
            filetypes=[
                (tr('PA9（形式は拡張子で決定）'), "*.pa9 *.epa9 *.ea9"),
                ("All files", "*"),
            ],
            confirmoverwrite=False,
            parent=self.root,
        )
        if not value:
            return False
        output = Path(value).expanduser()
        if not output.suffix:
            output = output.with_name(output.name + default_extension)
        if output.suffix.lower() not in {".pa9", ".epa9", ".ea9"}:
            self.messagebox.showerror(
                tr('PA9保存エラー'),
                tr('保存先は .pa9、.epa9、または .ea9 にしてください。'),
                parent=self.root,
            )
            return False
        try:
            expected_identity = snapshot_pa9_output_identity(output, overwrite=True)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.messagebox.showerror(
                tr('PA9保存エラー'), display_safe(exc, limit=2048), parent=self.root
            )
            return False
        if expected_identity is not None and not self.messagebox.askyesno(
            tr('上書き確認'),
            tr('既存ファイルを置き換えますか？\n{v0}', v0=output),
            default="no",
            parent=self.root,
        ):
            return False
        try:
            saved = document.save(
                output,
                overwrite=expected_identity is not None,
                expected_identity=expected_identity,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.messagebox.showerror(
                tr('PA9保存エラー'), display_safe(exc, limit=2048), parent=self.root
            )
            return False
        self.editor_path_var.set(str(saved))
        self.editor_form_dirty = False
        self._editor_update_controls()
        self._editor_update_status()
        self.status_var.set(tr('PA9を保存しました: {v0}', v0=saved))
        return True

    def _editor_confirm_leave(self) -> bool:
        if not self._editor_is_dirty():
            return True
        decision = self.messagebox.askyesnocancel(
            tr('未保存のPA9編集'),
            tr('編集内容を保存しますか？\n「いいえ」を選ぶと変更を破棄します。'),
            icon="warning",
            parent=self.root,
        )
        if decision is None:
            return False
        if decision:
            return self._editor_save_current(confirm=False)
        if self.editor_document is not None:
            self.editor_document.reset()
            self._editor_populate()
        self.editor_form_dirty = False
        return True

    def _file_row(self, parent: Any, row: int, text: str, variable: Any, browse: Any) -> Any:
        group = self.ttk.Frame(parent)
        group.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        group.columnconfigure(0, weight=1)
        self.ttk.Label(group, text=text).grid(row=0, column=0, columnspan=2, sticky="w")
        entry = self.ttk.Entry(group, textvariable=variable, width=18)
        entry.grid(row=1, column=0, sticky="ew")
        self.ttk.Button(group, text=tr('参照'), width=0, command=browse).grid(
            row=1, column=1, padx=(6, 0))
        return entry

    def _build_trade_tab(self, tab: Any) -> None:
        ttk = self.ttk
        tab.columnconfigure(0, weight=1)
        self.offer_entry = self._file_row(tab, 0, tr('送信するparty PA9'), self.offer_var, self._browse_offer)
        self.output_entry = self._file_row(tab, 1, tr('受信PA9の保存先'), self.output_var, self._browse_output)

        options = ttk.Frame(tab)
        options.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        timing = []
        for label, variable in ((tr('Timeout (秒)'), self.timeout_var),
                                (tr('終了猶予 (秒)'), self.settle_var)):
            group = ttk.Frame(options)
            ttk.Label(group, text=label).pack(anchor="w")
            ttk.Entry(group, textvariable=variable, width=8).pack(fill="x")
            timing.append(group)
        self._flow(options, timing)
        self.auto_confirm_check = ttk.Checkbutton(
            tab, text=tr('相手の個体を確認せず自動承認する'), variable=self.auto_confirm_var,
            style="Danger.TCheckbutton",
        )
        self.auto_confirm_check.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self._wrap(
            tab, text=(tr('通常は相手のspecies・nickname・checksumを表示してから承認します。 .epa9/.ea9は暗号化、.pa9は復号済みで保存します。')),
            style="Hint.TLabel",
        ).grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.trade_button = ttk.Button(self.task_action_bar, text=tr('交換を開始'), style="Accent.TButton", command=self._start_trade)

    def _build_probe_tab(self, tab: Any) -> None:
        ttk = self.ttk
        tab.columnconfigure(0, weight=1)
        self._file_row(tab, 0, tr('JSONL保存先'), self.capture_output_var, self._browse_capture_output)
        ttk.Label(tab, text=tr('採取時間 (秒)')).grid(
            row=1, column=0, sticky="w", pady=(10, 0)
        )
        ttk.Entry(tab, textvariable=self.capture_duration_var, width=10).grid(
            row=1, column=1, sticky="w", padx=8, pady=(10, 0)
        )
        self._wrap(
            tab, text=tr('同じ接続設定でPia datagramを受信し、あとからCLI/GUIで解析できるJSONLへ保存します。'),
            style="Hint.TLabel",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(14, 0))
        self.probe_button = ttk.Button(
            self.task_action_bar, text=tr('キャプチャを開始'), style="Accent.TButton", command=self._start_probe
        )

    def _build_inspect_tab(self, tab: Any) -> None:
        ttk = self.ttk
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(5, weight=1)
        self._file_row(tab, 0, tr('PA9ファイル'), self.inspect_var, self._browse_inspect)
        self.inspect_button = ttk.Button(tab, text=tr('PA9を検査'), command=self._inspect_pa9)
        self.inspect_button.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 14))

        ttk.Separator(tab).grid(row=2, column=0, columnspan=3, sticky="ew", pady=4)
        self._file_row(tab, 3, "Capture JSONL", self.analyze_var, self._browse_analyze)
        preview = ttk.Frame(tab)
        preview.grid(row=4, column=0, columnspan=3, sticky="ew")
        ttk.Label(preview, text="Preview").pack(side="left")
        ttk.Entry(preview, textvariable=self.preview_var, width=5).pack(side="left", padx=8)

        result_frame = ttk.Frame(tab)
        result_frame.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        result_frame.rowconfigure(1, weight=1)
        result_frame.columnconfigure(0, weight=1)
        self.analyze_button = ttk.Button(
            result_frame, text=tr('Captureを解析'), command=self._analyze_capture
        )
        self.analyze_button.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.result_text = self.scrolledtext.ScrolledText(
            result_frame, wrap="none", width=1, height=12, font="TkFixedFont", state="disabled",
            background="#ffffff", foreground="#20314b", relief="flat", borderwidth=0, padx=10, pady=8
        )
        self.result_text.grid(row=1, column=0, sticky="nsew")
        self.result_horizontal = ttk.Scrollbar(result_frame, orient="horizontal", command=self.result_text.xview)
        self.result_horizontal.grid(row=2, column=0, sticky="ew")
        self.result_text.configure(xscrollcommand=self.result_horizontal.set)

    def _browse_bss_helper(self) -> None:
        if not self._can_start_operation():
            return
        directory = self.filedialog.askdirectory(parent=self.root, title=tr("ビルド済みBSS補助フォルダーを選択"))
        if not directory:
            return
        try:
            from zasim.ldn.bss_seed import validate_bundle
            validate_bundle(directory)
        except (OSError, RuntimeError, ValueError) as exc:
            self.messagebox.showerror(tr("BSS補助を使用できません"), str(exc), parent=self.root)
            return
        if not self.bss_helper_var.get():
            self._bss_helper_previous_mode = next(key for key in ("自動", "はい", "いいえ")
                if tr(key) == self.single_interface_var.get())
        self.bss_helper_var.set(directory)
        self.single_interface_var.set(tr("はい"))
        self._update_backend_fields()

    def _clear_bss_helper(self) -> None:
        if not self._can_start_operation():
            return
        self.bss_helper_var.set("")
        self.single_interface_var.set(tr(self._bss_helper_previous_mode))
        self._update_backend_fields()

    def _connection(self) -> ConnectionSettings:
        direct = self.backend_var.get() == "direct"
        keys_path = self.keys_var.get().strip()
        mode = {tr('自動'): None, tr('はい'): True, tr('いいえ'): False}[self.single_interface_var.get()]
        helper = self.bss_helper_var.get().strip() if direct else ""
        if helper:
            mode = True
        selected = resolve_choice(self._selected_radio, self._radio_inventory) if direct else None
        if direct and keys_path:
            keys_path = str(Path(keys_path).expanduser())
        return ConnectionSettings(
            backend=self.backend_var.get(),
            relay_endpoint=self.relay_var.get().strip(),
            keys_path=keys_path,
            phyname=selected.phy if selected else self.phy_var.get().strip(),
            ifname=selected.interface if selected and mode is True else self.interface_var.get().strip(),
            nickname=self.nickname_var.get(),
            single_interface=mode,
            selected_radio=selected,
            bss_helper=helper or None,
        )

    def _backend_selected(self) -> None:
        self._update_backend_fields()
        if self.backend_var.get() == "direct":
            self._refresh_radios()

    def _refresh_radios(self) -> None:
        if not self._can_start_operation():
            return
        self._radio_inventory = read_inventory()
        # Retain only the same sysfs identity. Reordering, unplug/replug and
        # renamed devices must never select an unrelated replacement by index.
        selected = self._selected_radio
        self._selected_radio = next((r for r in self._radio_inventory.radios
                                     if selected is not None and r.key == selected.key), None)
        hint, self._initial_radio_hint = self._initial_radio_hint, None
        if selected is None and hint is not None:
            matches = [r for r in self._radio_inventory.radios if r.phy == hint[0]]
            exact = [r for r in matches if r.interface == hint[1]]
            if len(exact) == 1:
                self._selected_radio = exact[0]
            elif len(matches) == 1:
                self._selected_radio = matches[0]
        self._render_radio_picker()

    def _render_radio_picker(self) -> None:
        inventory = self._radio_inventory
        self.radio_combo.configure(values=tuple(r.label() for r in inventory.radios))
        selected = self._selected_radio
        self.phy_var.set(selected.phy if selected else "")
        self.radio_var.set(selected.label() if selected else tr('Wi-Fiを選択してください'))
        if inventory.status == "not_linux":
            notice = tr('このOSではWi-Fiを直接使えません。Linux側で接続を準備してください。')
        elif inventory.status in ("unknown", "not_loaded"):
            notice = tr('一覧を取得できていません。「一覧を更新」で再確認してください。')
        elif not inventory.radios:
            notice = tr('Wi-Fiが見つかりません。専用アダプターの接続とLinuxドライバを確認してください。')
        elif selected is not None and selected.state in ("in_use", "blocked"):
            notice = tr('このWi-Fiは使用中または無効です。接続を自動で切らず、別の専用機を選んでください。')
        else:
            notice = tr('インターネット用ではなく、交換専用のWi-Fiを選んでください。選択だけでは接続しません。')
        self.radio_notice.configure(text=notice)

    def _radio_selected(self, _event: Any = None) -> None:
        if not self._can_start_operation():
            self._render_radio_picker()
            return
        index = self.radio_combo.current()
        self._selected_radio = (self._radio_inventory.radios[index]
                                if 0 <= index < len(self._radio_inventory.radios) else None)
        self._render_radio_picker()

    def _check_environment(self) -> None:
        from .doctor import check_installation, format_report
        existing = getattr(self, "environment_window", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        report = check_installation(backend=self.backend_var.get())
        window = self.environment_window = self.tk.Toplevel(self.root)
        window.title(tr('環境チェック（読取り専用・接続試験なし）'))
        window.transient(self.root)
        width = max(240, min(760, self.root.winfo_width(), self.root.winfo_screenwidth() - 48))
        height = max(200, min(540, self.root.winfo_height(), self.root.winfo_screenheight() - 80))
        window.geometry(f"{width}x{height}")
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        text = self.environment_text = self.scrolledtext.ScrolledText(
            window, wrap="word", width=1, height=1, font="TkDefaultFont", padx=8, pady=8)
        text.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        def show(value):
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", value)
            text.configure(state="disabled")
        show(format_report(report))
        picker = self.ttk.Frame(window)
        picker.grid(row=1, column=0, sticky="ew", padx=8)
        picker.columnconfigure(0, weight=1)
        self.ttk.Label(picker, text=tr('調べるPHY（空欄は1台だけなら自動）')).grid(row=0, column=0, sticky="w")
        self.environment_phy = self.tk.StringVar(master=self.root, value=self.phy_var.get() if self.backend_var.get() == "direct" else "")
        self.ttk.Combobox(picker, textvariable=self.environment_phy, width=1,
                         values=sorted({r["phy"] for r in report["radios"]})).grid(row=1, column=0, sticky="ew")
        actions = self.ttk.Frame(window)
        actions.grid(row=2, column=0, sticky="ew", padx=8, pady=8)
        actions.columnconfigure((0, 1), weight=1, uniform="environment-actions")
        pending = [None]
        def detailed():
            if getattr(self, "_radio_diagnostic_thread", None) is not None and self._radio_diagnostic_thread.is_alive():
                show(tr('別の読取り診断が終了するまで待ってください。無線操作はしていません。'))
                return
            phy = self.environment_phy.get().strip() or None
            backend = self.backend_var.get()
            output = queue.SimpleQueue()
            def work():
                try:
                    output.put(format_report(check_installation(backend=backend, radio=True, phyname=phy)))
                except Exception as exc:
                    output.put(tr('読取り診断に失敗（未判定）: ') + type(exc).__name__)
            self.environment_radio_button.state(["disabled"])
            show(tr('このPCのカーネル能力・経路を読取り中。送受信・接続・設定変更はしません。'))
            try:
                self._radio_diagnostic_thread = threading.Thread(target=work, name="za-readonly-radio-check", daemon=True)
                self._radio_diagnostic_thread.start()
            except (OSError, RuntimeError) as exc:
                self._radio_diagnostic_thread = None
                self.environment_radio_button.state(["!disabled"])
                show(tr('読取り診断に失敗（未判定）: ') + type(exc).__name__)
                return
            def poll():
                pending[0] = None
                try:
                    value = output.get_nowait()
                except queue.Empty:
                    pending[0] = window.after(50, poll)
                    return
                show(value)
                self.environment_radio_button.state(["!disabled"])
            pending[0] = window.after(50, poll)
        self.environment_radio_button = self.ttk.Button(actions, text=tr('無線詳細（このPC）'), command=detailed)
        self.environment_radio_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.environment_close_button = self.ttk.Button(actions, text=tr('閉じる'), command=window.destroy)
        self.environment_close_button.grid(row=0, column=1, sticky="ew")
        def destroyed(event):
            if event.widget is window and pending[0] is not None:
                window.after_cancel(pending[0])
                pending[0] = None
        window.bind("<Destroy>", destroyed, add="+")
        window.bind("<Escape>", lambda _event: window.destroy())

    def _show_quickstart(self) -> None:
        existing = getattr(self, "help_window", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        window = self.help_window = self.tk.Toplevel(self.root)
        window.title(tr('交換の手順'))
        window.transient(self.root)
        width = min(760, self.root.winfo_width(), self.root.winfo_screenwidth() - 48)
        height = min(520, self.root.winfo_height(), self.root.winfo_screenheight() - 80)
        window.geometry(f"{max(240, width)}x{max(200, height)}")
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        self.help_text = self.scrolledtext.ScrolledText(
            window, wrap="word", width=1, height=1, font="TkDefaultFont", padx=8, pady=8
        )
        self.help_text.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        self.help_text.insert("1.0",
            tr('1. LinuxではWi-Fiを一覧から選択。WindowsではLinux側で準備した接続を使用（選択だけでは通信しません）。\n2. 実機Z-A 2.0.2でローカル通信の交換待受けを開く。\n3. 同じ8桁パスワード、送るparty PA9、受領ファイルの保存先を指定。\n4. 「交換を開始」。実機側でも渡してよい個体を選択・確定。\n5. PCに出る相手個体を確認して承認。完了表示まで待つ。\n\n実機の交換演出・セーブ表示中は電源を切らないでください。\n結果未確定の警告が出たら再交換せず、保存された受領データと実機を確認。\n無線の購入前: docs/WIRELESS_COMPATIBILITY.md（標準版の交換保証機種は未確定）\n環境ごとの詳しい手順: docs/USER_GUIDE.md')
        )
        self.help_text.configure(state="disabled")
        self.help_close_button = self.ttk.Button(window, text=tr('閉じる'), command=window.destroy)
        self.help_close_button.grid(row=1, column=0, sticky="e", padx=8, pady=(0, 8))
        window.bind("<Escape>", lambda _event: window.destroy())

    def _update_backend_fields(self) -> None:
        direct = self.backend_var.get() == "direct"
        advanced = bool(self.connection_advanced_var.get())
        if direct:
            self.radio_panel.pack(fill="x", before=self.connection_fields, pady=(6, 4))
        else:
            self.radio_panel.pack_forget()
        names = ["keys", "code"] if direct else ["code"]
        if advanced:
            names += ["nickname", "version"]
            if direct:
                reuse = bool(self.bss_helper_var.get()) or self.single_interface_var.get() == tr("はい")
                names += ["phy"] + ([] if reuse else ["interface"]) + ["reuse", "bss_helper"]
            else:
                names += ["relay"]
        visible = [self.connection_groups[name] for name in names]
        for group in self.connection_groups.values():
            group.grid_remove()
        self._flow_groups = [(parent, visible if parent is self.connection_fields else widgets, columns)
                             for parent, widgets, columns in self._flow_groups]
        for index, group in enumerate(visible):
            group.grid(row=index, column=0, sticky="ew", padx=4, pady=4)
        self.relay_entry.state(["disabled"] if direct else ["!disabled"])
        self.single_interface_combo.configure(state="readonly" if direct and not self.bss_helper_var.get() else "disabled")
        self.phy_entry.configure(state="readonly" if direct else "disabled")
        for widget in (self.keys_entry, self.keys_button, self.interface_entry):
            widget.state(["!disabled"] if direct else ["disabled"])
        self._render_radio_picker()
        self.connection_hint.configure(text=(
            tr('専用Wi-Fiを選択してください。通信にはLinuxの管理者権限と対応ドライバが必要です。') if direct else
            tr('Linux / WSL側でRelayを起動してください。鍵とWi-Fiは接続先で設定します。')
        ))
        self._schedule_layout()

    def _guard_direct_privilege(self) -> None:
        if (
            self.backend_var.get() == "direct"
            and os.name != "nt"
            and hasattr(os, "geteuid")
            and os.geteuid() != 0
        ):
            raise ValueError(
                tr('このPCの無線操作には管理者権限が必要です。通常ユーザーで使う場合は、管理者がLinux側で接続を準備し「準備済みの接続を使う」を選んでください。')
            )
        if self.backend_var.get() != "direct":
            return
        if sys.version_info < (3, 12):
            raise ValueError(
                tr('Linux直接接続にはPython 3.12以降が必要です。通常ユーザーGUI + relay方式も利用できます。')
            )
        try:
            import ldn  # noqa: F401
            import trio  # noqa: F401
        except ImportError as exc:
            raise ValueError(
                tr('Linux直接接続の依存が不足しています。requirements.txtをPython 3.12以降のvenvへインストールしてください。')
            ) from exc
        missing = [
            command
            for command in ("iw", "ip", "nmcli", "sysctl", "pkill")
            if shutil.which(command) is None
        ]
        if missing:
            raise ValueError(
                tr('Linux直接接続に必要なcommandがありません: ') + ", ".join(missing)
            )
        key_path = Path(self.keys_var.get().strip()).expanduser()
        if not key_path.is_file():
            raise ValueError(f"prod.keys file does not exist: {key_path}")

    def _confirm_replace(
        self, path: Path
    ) -> tuple[bool, bool, tuple[int, int, int, int] | None]:
        """Pin the exact existing file identity shown by the prompt."""

        identity = capture_file_identity(path)
        existed = identity is not None
        approved = not existed or self.messagebox.askyesno(
            tr('上書き確認'),
            tr('既存ファイルを置き換えますか？\n{v0}', v0=path),
            default="no",
            parent=self.root,
        )
        return bool(approved), existed, identity

    def _start_trade(self) -> None:
        if not self._can_start_operation():
            return
        try:
            if self._offline_busy():
                raise RuntimeError("capture analysis is already running")
            connection = self._connection()
            self._guard_direct_privilege()
            request = TradeRequest(
                connection=connection,
                title_version=self.title_version_var.get(),
                offer=Path(self.offer_var.get()).expanduser(),
                output=Path(self.output_var.get()).expanduser(),
                link_code=self.link_code_var.get(),
                timeout=float(self.timeout_var.get()),
                settle_grace=float(self.settle_var.get()),
                auto_confirm=bool(self.auto_confirm_var.get()),
            )
            request.validate()
            approved, existed, identity = self._confirm_replace(request.output)
            if not approved:
                return
            # Pin overwrite permission to the state the user actually saw.
            # If an absent target appears after this point, the worker's
            # no-clobber preflight rejects it instead of silently overwriting it.
            request = replace(
                request,
                overwrite=existed,
                expected_output_identity=identity if existed else None,
            )
            if request.auto_confirm and not self.messagebox.askyesno(
                tr('自動承認の確認'),
                tr('相手の個体を確認せず、取り消せない交換合意まで自動で進めます。続行しますか？'),
                icon="warning",
                default="no",
                parent=self.root,
            ):
                return
            job_id = self.controller.start_trade(request)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.messagebox.showerror(tr('開始できません'), str(exc), parent=self.root)
            return
        self.active_job_id, self.active_operation = job_id, "trade"
        self._try_append_log("[gui] trade started")
        self._set_live_busy(True, tr('交換を実行中'))

    def _start_probe(self) -> None:
        if not self._can_start_operation():
            return
        try:
            if self._offline_busy():
                raise RuntimeError("capture analysis is already running")
            connection = self._connection()
            self._guard_direct_privilege()
            request = ProbeRequest(
                connection=connection,
                output=Path(self.capture_output_var.get()).expanduser(),
                link_code=self.link_code_var.get(),
                duration=float(self.capture_duration_var.get()),
            )
            request.validate()
            approved, existed, identity = self._confirm_replace(request.output)
            if not approved:
                return
            request = replace(
                request,
                overwrite=existed,
                expected_output_identity=identity if existed else None,
            )
            job_id = self.controller.start_probe(request)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.messagebox.showerror(tr('開始できません'), str(exc), parent=self.root)
            return
        self.active_job_id, self.active_operation = job_id, "probe"
        self._try_append_log("[gui] capture started")
        self._set_live_busy(True, tr('キャプチャ中'))

    def _stop(self) -> None:
        stopped = self.controller.cancel()
        if self._offline_cancel is not None:
            self._offline_cancel.set()
            stopped = True
        if stopped:
            self.status_var.set(tr('停止要求を送信しました。radio cleanupを待っています…'))
            self.stop_button.state(["disabled"])

    def _set_live_busy(self, busy: bool, status: str | None = None) -> None:
        state = ["disabled"] if busy else ["!disabled"]
        self.trade_button.state(state)
        self.probe_button.state(state)
        self.analyze_button.state(state)
        if busy:
            self.stop_button.state(["!disabled"])
            self.progress.start(12)
        else:
            self.stop_button.state(["disabled"])
            self.progress.stop()
        self.status_var.set(status or tr('待機中'))
        for widget in (self.radio_refresh_button, self.relay_radio, self.connection_advanced_check,
                       self.bss_helper_browse, self.bss_helper_clear):
            widget.state(state)
        self.direct_radio.state(["disabled"] if busy or not sys.platform.startswith("linux") else ["!disabled"])
        self.radio_combo.configure(state="disabled" if busy else "readonly")

    def _handle_confirmation(self, event: GuiEvent) -> None:
        prompt = event.payload
        if not isinstance(prompt, ConfirmationPrompt):
            self.controller.cancel()
            return
        if not self.controller.is_confirmation_pending(prompt):
            return
        if self.close_when_idle or self.controller.state == ControllerState.STOPPING:
            self.controller.resolve_confirmation(prompt, False)
            return
        try:
            record = PA9(prompt.offer, encrypted=True)
            detail = (
                tr('Species (internal): {v0}\nNickname: {v1}\nLevel: {v2}\nChecksum: {v3}\n\nこの交換を確定しますか？確定後は取り消せません。', v0=record.species_internal, v1=display_safe(record.nickname or '(none)', limit=64), v2=record.stat_level, v3='OK' if record.checksum_valid else 'INVALID')
            )
        except (TypeError, ValueError) as exc:
            detail = tr('相手のPA9を表示できませんでした: {v0}\n交換を拒否します。', v0=exc)
            try:
                self.messagebox.showerror(tr('相手PA9エラー'), detail, parent=self.root)
            finally:
                self.controller.resolve_confirmation(prompt, False)
            return
        try:
            decision = self.messagebox.askyesno(
                tr('交換相手の確認'),
                detail,
                icon="question",
                default="no",
                parent=self.root,
            )
        except BaseException as exc:
            # The confirmation event has already been consumed.  Fail closed
            # so a display/encoding error cannot leave the trade waiting.
            self._try_append_log(
                "[gui] confirmation dialog failed; rejecting offer: "
                f"{type(exc).__name__}: {display_safe(exc)}"
            )
            self.controller.resolve_confirmation(prompt, False)
            return
        self.controller.resolve_confirmation(prompt, bool(decision))

    def _handle_terminal(self, event: GuiEvent) -> None:
        operation = self.active_operation
        if event.kind == "succeeded":
            if operation == "trade" and isinstance(event.payload, LiveTradeResult):
                if event.payload.completed:
                    message = tr('交換が完了しました。')
                    if event.payload.native_save_sync_complete:
                        message += tr('\nゲームとの保存同期と、PCの受領データ保存も完了しました。')
                    message += tr('\n実機の交換演出・セーブ表示が終わるまで電源を切らないでください。')
                    title = tr('完了')
                else:
                    message = (
                        tr('FinalAgreement送信後に通信が終了したため、ゲーム機側の結果は未確定です。再交換せず、双方のゲーム画面を確認してください。')
                    )
                    title = tr('結果の確認が必要')
                message += tr('\n保存先: {v0}', v0=event.payload.output_path)
                if event.payload.warning:
                    message += tr('\n警告: {v0}', v0=event.payload.warning)
            else:
                message = tr('処理が完了しました。')
                title = tr('完了')
                if isinstance(event.payload, dict):
                    message += "\n" + json.dumps(event.payload, ensure_ascii=False, indent=2)
            self._try_append_log(f"[gui] {message}")
            if operation == "trade" and isinstance(event.payload, LiveTradeResult) and not event.payload.completed:
                self.messagebox.showwarning(title, message, parent=self.root)
            else:
                self.messagebox.showinfo(title, message, parent=self.root)
        elif event.kind == "declined":
            safe_payload = display_safe(event.payload, limit=2048)
            self._try_append_log(f"[gui] trade declined: {safe_payload}")
            self.messagebox.showinfo(tr('交換を拒否しました'), safe_payload, parent=self.root)
        elif event.kind == "cancelled":
            safe_payload = display_safe(event.payload, limit=2048)
            if operation == "probe":
                safe_payload += (
                    tr('\nキャプチャ開始後に停止した場合、記録済みのJSONLは保存先に残ります。')
                )
            self._try_append_log(f"[gui] cancelled: {safe_payload}")
            if not self.close_when_idle:
                self.messagebox.showinfo(tr('停止しました'), safe_payload, parent=self.root)
        else:
            safe_payload = display_safe(event.payload, limit=2048)
            self._try_append_log(f"[gui] failed: {safe_payload}")
            self.messagebox.showerror(tr('エラー'), safe_payload, parent=self.root)
        self.controller.acknowledge(event.job_id)
        self.active_job_id = None
        self.active_operation = None
        self._set_live_busy(False)

    def _poll(self) -> None:
        delay = self.POLL_MS
        try:
            if self._pending_terminal_event is not None:
                self._handle_terminal(self._pending_terminal_event)
                self._pending_terminal_event = None
            # Keep the remainder of a drained batch across presentation errors.
            # Do not accumulate further batches while one is still pending.
            if not self._pending_controller_events and not self._pending_log_lines:
                self._pending_controller_events.extend(self.controller.drain_events())
            deadline = time.monotonic() + self.POLL_BUDGET_SECONDS
            while self._pending_controller_events or self._pending_log_lines:
                if self._pending_log_lines or self._pending_controller_events[0].kind == "log":
                    self._present_log_slice(deadline)
                    # Always yield between log slices, including a single large
                    # multiline message. Never drain another controller batch
                    # into this backlog or let a prompt/result overtake it.
                    if self._pending_log_lines or (
                        self._pending_controller_events
                        and self._pending_controller_events[0].kind == "log"
                    ):
                        break
                    if not self._pending_controller_events:
                        break
                if time.monotonic() >= deadline:
                    break
                event = self._pending_controller_events.popleft()
                if self.active_job_id is not None and event.job_id != self.active_job_id:
                    continue
                if event.kind == "confirmation":
                    self._handle_confirmation(event)
                else:
                    # Retain an undelivered result across native dialog errors.
                    # A later poll retries presentation; close cannot discard it.
                    self._pending_terminal_event = event
                    self._handle_terminal(event)
                    self._pending_terminal_event = None
            while True:
                if self._pending_offline_event is None:
                    try:
                        self._pending_offline_event = self._offline_events.get_nowait()
                    except queue.Empty:
                        break
                kind, payload = self._pending_offline_event
                if kind == "result":
                    self._show_result(payload)
                    self.status_var.set(tr('解析完了'))
                elif kind == "cancelled":
                    self.status_var.set(tr('解析を停止しました'))
                    self._try_append_log("[gui] capture analysis cancelled")
                else:
                    self.status_var.set(tr('解析失敗'))
                    self.messagebox.showerror(
                        tr('解析エラー'),
                        display_safe(payload, limit=2048),
                        parent=self.root,
                    )
                # Worker termination is not successful result presentation.
                self._pending_offline_event = None
                self._offline_thread = None
                self._offline_cancel = None
                self.analyze_button.state(["!disabled"])
                if not self.controller.is_busy:
                    self.trade_button.state(["!disabled"])
                    self.probe_button.state(["!disabled"])
                    self.stop_button.state(["disabled"])
                    self.progress.stop()
            if self.close_when_idle and not self.controller.is_busy and not self._pending_results():
                self._destroy_root()
                return
            if self._pending_controller_events or self._pending_log_lines:
                delay = self.BACKLOG_POLL_MS
        except Exception as exc:
            # Keep the GUI event pump alive even if presentation of one result
            # fails; live cleanup continues in the controller worker.
            self._try_append_log(f"[gui] event handler failed: {type(exc).__name__}: {exc}")
        self.root.after(delay, self._poll)

    def _present_log_slice(self, deadline: float) -> None:
        lines: list[str] = []
        characters = consumed = 0
        while len(lines) < self.LOG_SLICE_LINES and characters < self.LOG_SLICE_CHARS:
            if time.monotonic() >= deadline:
                break
            if not self._pending_log_lines:
                if not self._pending_controller_events or consumed >= self.LOG_SLICE_LINES:
                    break
                event = self._pending_controller_events[0]
                if event.kind != "log":
                    break
                self._pending_controller_events.popleft()
                consumed += 1
                if self.active_job_id is not None and event.job_id != self.active_job_id:
                    continue
                self._pending_log_lines.extend(str(event.payload).rstrip().splitlines() or [""])
            line = display_safe(self._pending_log_lines.popleft(), limit=4096) + "\n"
            lines.append(line)
            characters += len(line)
        if lines:
            try:
                self._append_log_lines(lines)
            except Exception:
                # Logs are ancillary; a broken widget must not strand the
                # following prompt/result or stop worker cleanup polling.
                pass

    def _try_append_log(self, text: str) -> None:
        # Logging is ancillary. A damaged log widget must neither hide a
        # terminal dialog nor kill the polling loop used to await cleanup.
        try:
            self._append_log(text)
        except Exception:
            pass

    def _append_log(self, text: str) -> None:
        lines = str(text).rstrip().splitlines() or [""]
        self._append_log_lines([display_safe(line, limit=4096) + "\n" for line in lines])

    def _append_log_lines(self, lines: list[str]) -> None:
        # One Tcl insertion/scroll/state transition per bounded display slice,
        # not per message. Preserve per-line escaping and the existing tail cap.
        self.log_text.configure(state="normal")
        try:
            self.log_text.insert("end", "".join(lines))
            self._log_lines += len(lines)
            if self._log_lines > self.MAX_LOG_LINES:
                remove = self._log_lines - self.MAX_LOG_LINES
                self.log_text.delete("1.0", f"{remove + 1}.0")
                self._log_lines = self.MAX_LOG_LINES
            self.log_text.see("end")
        finally:
            self.log_text.configure(state="disabled")

    def _show_result(self, value: object) -> None:
        rendered = json.dumps(value, ensure_ascii=True, indent=2)
        self.result_text.configure(state="normal")
        try:
            self.result_text.delete("1.0", "end")
            self.result_text.insert("1.0", rendered)
        finally:
            self.result_text.configure(state="disabled")

    def _inspect_pa9(self) -> None:
        try:
            path = Path(self.inspect_var.get()).expanduser()
            if not path.is_file():
                raise ValueError(f"PA9 file does not exist: {path}")
            self._show_result(_pa9_summary(path))
            self.status_var.set(tr('PA9検査完了'))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.messagebox.showerror(tr('PA9検査エラー'), str(exc), parent=self.root)

    def _analyze_capture(self) -> None:
        if not self._can_start_operation():
            return
        try:
            path = Path(self.analyze_var.get()).expanduser()
            if not path.is_file():
                raise ValueError(f"capture file does not exist: {path}")
            preview = int(self.preview_var.get(), 10)
            if not 0 <= preview <= 4096:
                raise ValueError("preview must be from 0 through 4096")
        except (OSError, TypeError, ValueError) as exc:
            self.messagebox.showerror(tr('解析エラー'), str(exc), parent=self.root)
            return

        cancel_event = threading.Event()

        def work() -> None:
            try:
                result = _collect_capture_analysis(
                    path,
                    preview=preview,
                    cancel_event=cancel_event,
                )
            except OperationCancelled as exc:
                self._offline_events.put(("cancelled", exc))
            except BaseException as exc:
                self._offline_events.put(("error", exc))
            else:
                self._offline_events.put(("result", result))

        self.analyze_button.state(["disabled"])
        self.trade_button.state(["disabled"])
        self.probe_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.progress.start(12)
        self.status_var.set(tr('Captureを解析中'))
        self._offline_cancel = cancel_event
        try:
            self._offline_thread = threading.Thread(
                target=work, name="za-gui-analysis", daemon=False
            )
            self._offline_thread.start()
        except (OSError, RuntimeError) as exc:
            self._offline_thread = None
            self._offline_cancel = None
            self._set_live_busy(False)
            self.messagebox.showerror(tr('解析エラー'), str(exc), parent=self.root)

    def _offline_busy(self) -> bool:
        return self._offline_thread is not None and self._offline_thread.is_alive()

    def _pending_results(self) -> bool:
        """Include finished workers whose terminal event the UI has not shown."""
        return (
            getattr(self, "active_job_id", None) is not None
            or getattr(self, "_pending_terminal_event", None) is not None
            or getattr(self, "_pending_offline_event", None) is not None
            or bool(getattr(self.controller, "has_pending_result", False))
            or getattr(self, "_offline_thread", None) is not None
        )

    def _can_start_operation(self) -> bool:
        # Disabled buttons are presentation, not a reentrant-callback guard.
        return not (
            getattr(self, "close_when_idle", False)
            or bool(getattr(self.controller, "is_busy", False))
            or self._pending_results()
        )

    def _request_close(self) -> None:
        if self.close_when_idle:
            return
        busy = self.controller.is_busy or self._offline_busy()
        if not busy:
            if not self._editor_confirm_leave():
                return
            if self._pending_results():
                # The normal poll must present/acknowledge terminal results
                # (especially indeterminate trade warnings) before destroy.
                self.close_when_idle = True
                self._editor_update_controls()
                self.status_var.set(tr('処理結果の確認後に終了します…'))
            else:
                self._destroy_root()
            return
        if not self.messagebox.askyesno(
            tr('終了確認'),
            tr('実行中の処理を停止し、radio cleanup完了後に終了しますか？'),
            icon="warning",
            default="no",
            parent=self.root,
        ):
            return
        if not self._editor_confirm_leave():
            return
        self.close_when_idle = True
        self.controller.cancel()
        if self._offline_cancel is not None:
            self._offline_cancel.set()
        self._editor_update_controls()
        self.status_var.set(tr('cleanup完了を待って終了します…'))
        self.stop_button.state(["disabled"])

    def _cancel_callbacks(self) -> None:
        # This app owns its Tk interpreter. Timers can otherwise outlive widgets
        # and fire into deleted commands when another Tk root is opened later.
        for callback in self.root.tk.call("after", "info"):
            # Some native ttk timers have a Tcl list script, not the name of a
            # registered Python command. tkinter.after_cancel() tries to delete
            # that script as a command (TypeError on modern Tcl). Cancel the
            # timer itself; widget destruction releases Python registrations.
            self.root.tk.call("after", "cancel", callback)

    def _root_destroyed(self, event: Any) -> None:
        if event.widget is self.root:
            self._dispose_editor_traces()
            self._cancel_callbacks()

    def _dispose_editor_traces(self) -> None:
        # Tcl owns these Python closures even after its widgets disappear.
        # Remove only our registrations, never foreign traces/interpreters.
        traces, self._editor_traces = self._editor_traces, []
        for variable, mode, callback in traces:
            try:
                variable.trace_remove(mode, callback)
            except self.tk.TclError:
                pass  # already removed along with the Tcl variable/command

    def _destroy_root(self) -> None:
        self.progress.stop()
        self._dispose_editor_traces()
        self._cancel_callbacks()
        self.root.destroy()

    def _browse_keys(self) -> None:
        value = self.filedialog.askopenfilename(title=tr('prod.keysを選択'), parent=self.root)
        if value:
            self.keys_var.set(value)

    def _browse_offer(self) -> None:
        value = self.filedialog.askopenfilename(
            title=tr('送信するparty PA9を選択'),
            filetypes=[("PA9", "*.epa9 *.ea9 *.pa9"), ("All files", "*")],
            parent=self.root,
        )
        if value:
            self.offer_var.set(value)
            if not self.output_var.get():
                self.output_var.set(str(self._next_received_path(Path(value).parent)))

    @staticmethod
    def _next_received_path(directory: Path) -> Path:
        for index in range(10000):
            name = "received.epa9" if index == 0 else f"received-{index:03d}.epa9"
            candidate = directory / name
            if not os.path.lexists(candidate):
                return candidate
        raise ValueError(tr('受領ファイルが多すぎます。別の保存先を選択してください。'))

    def _browse_output(self) -> None:
        value = self.filedialog.asksaveasfilename(
            title=tr('受信PA9の保存先'),
            defaultextension=".epa9",
            filetypes=[
                ("Encrypted PA9", "*.epa9 *.ea9"),
                ("Decrypted PA9", "*.pa9"),
            ],
            confirmoverwrite=False,
            parent=self.root,
        )
        if value:
            self.output_var.set(value)

    def _browse_capture_output(self) -> None:
        value = self.filedialog.asksaveasfilename(
            title=tr('Capture JSONLの保存先'),
            defaultextension=".jsonl",
            filetypes=[("JSON Lines", "*.jsonl")],
            confirmoverwrite=False,
            parent=self.root,
        )
        if value:
            self.capture_output_var.set(value)

    def _browse_inspect(self) -> None:
        value = self.filedialog.askopenfilename(
            title=tr('PA9を選択'),
            filetypes=[("PA9", "*.epa9 *.ea9 *.pa9"), ("All files", "*")],
            parent=self.root,
        )
        if value:
            self.inspect_var.set(value)

    def _browse_analyze(self) -> None:
        value = self.filedialog.askopenfilename(
            title=tr('Capture JSONLを選択'),
            filetypes=[("JSON Lines", "*.jsonl"), ("All files", "*")],
            parent=self.root,
        )
        if value:
            self.analyze_var.set(value)


def launch_gui(
    *,
    backend: str | None = None,
    relay: str = "127.0.0.1:24872",
    keys: str = "~/.switch/prod.keys",
    phyname: str = "",
    ifname: str = "ldnclient",
    nickname: str = "ZA-Trade",
    title_version: str = DEFAULT_ZA_TITLE_VERSION,
    language: str | None = None,
) -> int:
    """Create and run the desktop UI, with actionable Tk/display errors."""

    selected_language = preferred_language() if language is None else validate_language(language)
    identification_token_for_title(title_version)
    if backend is None:
        backend = "direct" if sys.platform.startswith("linux") and os.name != "nt" else "relay"
    if backend not in ("direct", "relay"):
        raise ValueError("GUI backend must be 'direct' or 'relay'")
    if os.name == "nt" and backend == "direct":
        raise ValueError("Windows GUI requires the relay backend")

    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk
    except (ImportError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            "Tk is not installed. On Ubuntu/Debian install python3-tk; "
            "the official Windows Python installer includes Tk."
        ) from exc

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise RuntimeError(
            "Tk could not open a desktop display. Start the GUI from a Windows "
            "desktop or a Linux Wayland/X11 session with DISPLAY configured."
        ) from exc
    try:
        _DesktopApp(
            root,
            tk=tk,
            ttk=ttk,
            filedialog=filedialog,
            messagebox=messagebox,
            scrolledtext=scrolledtext,
            backend=backend,
            relay=relay,
            keys=keys,
            phyname=phyname,
            ifname=ifname,
            nickname=nickname,
            title_version=title_version,
            language=selected_language,
        )
        root.mainloop()
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass
    return 0


__all__ = ["ConnectionSettings", "GuiController", "launch_gui"]
