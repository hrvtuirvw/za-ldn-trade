from __future__ import annotations

import ast
import builtins
import inspect
import os
from pathlib import Path
import threading
import textwrap
from types import SimpleNamespace

import pytest

import zasim.gui as za_gui
from zasim.control import OperationCancelled
from zasim.text import display_safe


def _create_tk_root(tkinter):
    """Create a test root, retrying Windows' intermittent Tcl init failure."""

    last_error = None
    for _attempt in range(3 if os.name == "nt" else 1):
        try:
            return tkinter.Tk()
        except tkinter.TclError as exc:
            last_error = exc
    if os.environ.get("ZA_REQUIRE_GUI_TESTS") == "1":
        raise last_error
    pytest.skip(f"desktop display unavailable: {last_error}")


def test_required_ci_display_cannot_silently_skip_after_startup_retries(monkeypatch):
    class StartupError(Exception):
        pass
    calls = []
    def missing():
        calls.append(True)
        raise StartupError('synthetic unavailable Tk')
    monkeypatch.setenv('ZA_REQUIRE_GUI_TESTS', '1')
    with pytest.raises(StartupError, match='synthetic unavailable Tk'):
        _create_tk_root(SimpleNamespace(Tk=missing, TclError=StartupError))
    assert len(calls) == (3 if os.name == 'nt' else 1)


def test_gui_reports_missing_tk_without_affecting_other_modules(monkeypatch) -> None:
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "tkinter" or name.startswith("tkinter."):
            raise ModuleNotFoundError("synthetic missing Tk")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(RuntimeError, match="python3-tk"):
        za_gui.launch_gui()


def test_windows_gui_rejects_direct_backend_before_creating_root(monkeypatch) -> None:
    monkeypatch.setattr(za_gui, "os", SimpleNamespace(name="nt"))
    with pytest.raises(ValueError, match="requires the relay"):
        za_gui.launch_gui(backend="direct")


def test_display_safe_escapes_surrogates_controls_and_bidi() -> None:
    rendered = display_safe("ok\ud800\n\u202Espoof")
    assert rendered == "ok\\uD800\\u000A\\u202Espoof"
    rendered.encode("utf-8")


def test_gui_yes_no_safety_prompts_default_to_no() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(za_gui._DesktopApp)))
    prompts = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "askyesno"
    ]

    assert prompts
    for prompt in prompts:
        defaults = [
            keyword.value
            for keyword in prompt.keywords
            if keyword.arg == "default"
        ]
        assert len(defaults) == 1
        assert isinstance(defaults[0], ast.Constant)
        assert defaults[0].value == "no"


@pytest.mark.parametrize("operation", ["trade", "probe"])
def test_gui_does_not_gain_overwrite_permission_after_confirmation_race(
    operation, tmp_path, monkeypatch
) -> None:
    target = tmp_path / ("received.epa9" if operation == "trade" else "capture.jsonl")
    competing = b"created after the confirmation snapshot"
    captured = []

    class Controller:
        def start_trade(self, request):
            captured.append(request)
            return 1

        def start_probe(self, request):
            captured.append(request)
            return 1

    class Messages:
        def showerror(self, *_args, **_kwargs):
            pytest.fail("the request should remain valid")

    def approve_absent_then_race(_self, path):
        assert path == target
        path.write_bytes(competing)
        return True, False, None

    monkeypatch.setattr(za_gui._DesktopApp, "_offline_busy", lambda _self: False)
    monkeypatch.setattr(za_gui._DesktopApp, "_guard_direct_privilege", lambda _self: None)
    monkeypatch.setattr(
        za_gui._DesktopApp,
        "_connection",
        lambda _self: za_gui.ConnectionSettings(backend="relay"),
    )
    monkeypatch.setattr(za_gui._DesktopApp, "_confirm_replace", approve_absent_then_race)
    monkeypatch.setattr(za_gui._DesktopApp, "_append_log", lambda *_args: None)
    monkeypatch.setattr(za_gui._DesktopApp, "_set_live_busy", lambda *_args: None)

    app = object.__new__(za_gui._DesktopApp)
    app.root = object()
    app.controller = Controller()
    app.messagebox = Messages()
    app.output_var = SimpleNamespace(get=lambda: str(target))
    app.capture_output_var = SimpleNamespace(get=lambda: str(target))
    app.timeout_var = SimpleNamespace(get=lambda: "120")
    app.settle_var = SimpleNamespace(get=lambda: "5")
    app.auto_confirm_var = SimpleNamespace(get=lambda: False)
    app.capture_duration_var = SimpleNamespace(get=lambda: "30")
    app.link_code_var = SimpleNamespace(get=lambda: "12345678")
    app.title_version_var = SimpleNamespace(get=lambda: "2.0.2")
    offer = tmp_path / "offer.pa9"
    offer.write_bytes(b"structural validation happens in the worker")
    app.offer_var = SimpleNamespace(get=lambda: str(offer))

    if operation == "trade":
        app._start_trade()
    else:
        app._start_probe()

    assert len(captured) == 1
    assert captured[0].link_code == "12345678"
    assert captured[0].overwrite is False
    assert captured[0].expected_output_identity is None
    assert target.read_bytes() == competing


def test_gui_replace_confirmation_snapshots_identity_before_prompt(
    tmp_path
) -> None:
    target = tmp_path / "received.epa9"
    target.write_bytes(b"version shown when the prompt opened")

    class Messages:
        def askyesno(self, *_args, **_kwargs):
            target.write_bytes(b"replacement created while prompt was open")
            return True

    app = object.__new__(za_gui._DesktopApp)
    app.root = object()
    app.messagebox = Messages()

    approved, existed, identity = app._confirm_replace(target)

    assert approved is True
    assert existed is True
    assert identity is not None
    assert identity != za_gui.capture_file_identity(target)


def test_gui_capture_analysis_is_bounded(monkeypatch, tmp_path) -> None:
    path = tmp_path / "capture.jsonl"
    path.write_text("unused", encoding="utf-8")
    monkeypatch.setattr(
        za_gui,
        "analyze_capture",
        lambda *_args, **_kwargs: iter([{"n": 1}, {"n": 2}, {"n": 3}]),
    )

    result = za_gui._collect_capture_analysis(
        path, preview=48, cancel_event=threading.Event(), limit=2
    )

    assert result[:2] == [{"n": 1}, {"n": 2}]
    assert result[-1]["truncated"] is True


def test_gui_capture_analysis_honors_cancellation(monkeypatch, tmp_path) -> None:
    path = tmp_path / "capture.jsonl"
    path.write_text("unused", encoding="utf-8")
    monkeypatch.setattr(
        za_gui, "analyze_capture", lambda *_args, **_kwargs: iter([{"n": 1}])
    )
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(OperationCancelled):
        za_gui._collect_capture_analysis(
            path, preview=48, cancel_event=cancelled, limit=2
        )


def test_gui_pa9_summary_rejects_large_file_before_read(tmp_path) -> None:
    path = Path(tmp_path) / "large.pa9"
    path.write_bytes(bytes(1024 * 1024))

    with pytest.raises(ValueError, match="exactly"):
        za_gui._pa9_summary(path)


def test_pa9_editor_hidden_tk_smoke() -> None:
    tkinter = pytest.importorskip("tkinter")
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    from zasim.pa9 import PA9
    from zasim.pa9_editor import FIELD_SPECS, PA9EditorDocument

    root = _create_tk_root(tkinter)
    root.withdraw()
    try:
        app = za_gui._DesktopApp(
            root,
            tk=tkinter,
            ttk=ttk,
            filedialog=filedialog,
            messagebox=messagebox,
            scrolledtext=scrolledtext,
            backend="relay",
            relay="127.0.0.1:24872",
            keys="unused",
            phyname="phy0",
            ifname="ldnclient",
            nickname="GUI-Test",
        )
        assert set(app.editor_vars) == set(FIELD_SPECS)
        assert app.link_code_var.get() == ""
        assert app.link_code_entry.cget("show") == "*"

        party = PA9()
        party.species_internal = 25
        party.nature = 0
        party.stat_nature = 0
        party.gender = 0
        party.stat_level = 50
        party.stat_hp_max = 100
        party.update_checksum()
        app.editor_document = PA9EditorDocument(party)
        app.editor_path_var.set("synthetic.pa9")
        app._editor_populate()
        app.editor_vars["nickname"].set("GUI-EDIT")
        app.editor_vars["species_internal"].set("133")
        assert app.editor_form_dirty
        assert app._editor_apply_pending()
        assert app.editor_document.record.nickname == "GUI-EDIT"
        assert app.editor_document.record.species_internal == 133
        assert app.editor_document.dirty
        assert app.notebook.tab(app.editor_tab, "text").endswith("*")
        for name in ("ev_hp", "ev_atk", "ev_def", "ev_spa", "ev_spd", "ev_spe"):
            app.editor_vars[name].set("001")
        assert app.editor_ev_total_var.get() == "EV合計: 6"

        stored = PA9(party.to_bytes(party=False))
        app.editor_document = PA9EditorDocument(stored)
        app._editor_populate()
        assert all("disabled" in widget.state() for widget in app.editor_party_widgets)
        root.update_idletasks()
    finally:
        root.destroy()


def test_close_during_live_work_preserves_or_freezes_editor_state() -> None:
    tkinter = pytest.importorskip("tkinter")
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    from zasim.pa9 import PA9
    from zasim.pa9_editor import PA9EditorDocument

    root = _create_tk_root(tkinter)
    root.withdraw()

    class FakeController:
        is_busy = True

        def __init__(self) -> None:
            self.cancel_calls = 0

        def cancel(self) -> bool:
            self.cancel_calls += 1
            return True

    class Dialogs:
        def __init__(self, close: bool, editor: bool | None = None) -> None:
            self.close = close
            self.editor = editor
            self.editor_prompts = 0

        def askyesno(self, *_args, **_kwargs) -> bool:
            return self.close

        def askyesnocancel(self, *_args, **_kwargs) -> bool | None:
            self.editor_prompts += 1
            return self.editor

    try:
        app = za_gui._DesktopApp(
            root,
            tk=tkinter,
            ttk=ttk,
            filedialog=filedialog,
            messagebox=messagebox,
            scrolledtext=scrolledtext,
            backend="relay",
            relay="127.0.0.1:24872",
            keys="unused",
            phyname="phy0",
            ifname="ldnclient",
            nickname="GUI-Test",
        )
        record = PA9()
        record.species_internal = 25
        record.nature = 0
        record.stat_nature = 0
        record.gender = 0
        record.stat_level = 50
        record.stat_hp_max = 100
        record.update_checksum()
        app.editor_document = PA9EditorDocument(record)
        app._editor_populate()
        app.editor_vars["nickname"].set("unsaved")
        assert app._editor_is_dirty()

        controller = FakeController()
        app.controller = controller
        decline_close = Dialogs(close=False)
        app.messagebox = decline_close
        app._request_close()
        assert app._editor_is_dirty()
        assert decline_close.editor_prompts == 0
        assert controller.cancel_calls == 0

        cancel_editor = Dialogs(close=True, editor=None)
        app.messagebox = cancel_editor
        app._request_close()
        assert app._editor_is_dirty()
        assert not app.close_when_idle
        assert cancel_editor.editor_prompts == 1
        assert controller.cancel_calls == 0

        accept_and_discard = Dialogs(close=True, editor=False)
        app.messagebox = accept_and_discard
        app._request_close()
        assert app.close_when_idle
        assert not app._editor_is_dirty()
        assert controller.cancel_calls == 1
        assert "disabled" in app.editor_open_button.state()
        assert all("disabled" in widget.state() for widget in app.editor_input_widgets)
        assert all("disabled" in widget.state() for widget in app.editor_helper_buttons)
    finally:
        root.destroy()


def test_pa9_editor_validation_keeps_checksum_warning_and_noop_is_clean() -> None:
    tkinter = pytest.importorskip("tkinter")
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    from zasim.pa9 import PA9
    from zasim.pa9_editor import PA9EditorDocument

    class DialogRecorder:
        def __init__(self) -> None:
            self.errors: list[tuple[str, str]] = []
            self.warnings: list[tuple[str, str]] = []
            self.infos: list[tuple[str, str]] = []

        def showerror(self, title: str, detail: str, **_kwargs) -> None:
            self.errors.append((title, detail))

        def showwarning(self, title: str, detail: str, **_kwargs) -> None:
            self.warnings.append((title, detail))

        def showinfo(self, title: str, detail: str, **_kwargs) -> None:
            self.infos.append((title, detail))

    root = _create_tk_root(tkinter)
    root.withdraw()
    try:
        app = za_gui._DesktopApp(
            root,
            tk=tkinter,
            ttk=ttk,
            filedialog=filedialog,
            messagebox=messagebox,
            scrolledtext=scrolledtext,
            backend="relay",
            relay="127.0.0.1:24872",
            keys="unused",
            phyname="phy0",
            ifname="ldnclient",
            nickname="GUI-Test",
        )
        record = PA9()
        record.species_internal = 25
        record.nature = 0
        record.stat_nature = 0
        record.gender = 0
        record.stat_level = 50
        record.stat_hp_max = 100
        record.update_checksum()

        dialogs = DialogRecorder()
        app.messagebox = dialogs
        record.checksum ^= 1
        app.editor_document = PA9EditorDocument(record)
        app._editor_populate()
        app._editor_validate()
        assert not dialogs.errors
        assert not dialogs.infos
        assert len(dialogs.warnings) == 1
        assert "checksum" in dialogs.warnings[0][1]

        record.update_checksum()
        app.editor_document = PA9EditorDocument(record)
        app._editor_populate()
        app._editor_apply()
        assert not app._editor_is_dirty()
        assert "未保存" not in app.status_var.get()
        assert "変更はありません" in app.status_var.get()
    finally:
        root.destroy()


def test_pa9_editor_reload_uses_save_as_path_and_suffix_controls_format(
    tmp_path: Path,
) -> None:
    tkinter = pytest.importorskip("tkinter")
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    from zasim.pa9 import PA9
    from zasim.pa9_editor import PA9EditorDocument

    class SaveDialogs:
        def __init__(self, *values: Path) -> None:
            self.values = [str(value) for value in values]
            self.calls: list[dict[str, object]] = []

        def asksaveasfilename(self, **kwargs) -> str:
            self.calls.append(kwargs)
            return self.values.pop(0)

    class Messages:
        def askyesnocancel(self, *_args, **_kwargs) -> bool:
            return True

        def askyesno(self, *_args, **_kwargs) -> bool:
            return True

        def showerror(self, title: str, detail: str, **_kwargs) -> None:
            pytest.fail(f"unexpected {title}: {detail}")

    record = PA9()
    record.species_internal = 25
    record.nickname = "BEFORE"
    record.nature = 0
    record.stat_nature = 0
    record.gender = 0
    record.stat_level = 50
    record.stat_hp_max = 100
    record.update_checksum()
    source = tmp_path / "source.bin"
    source.write_bytes(record.to_bytes(encrypted=False))
    explicit_encrypted = tmp_path / "saved.epa9"
    extensionless = tmp_path / "second-save"

    root = _create_tk_root(tkinter)
    root.withdraw()
    try:
        app = za_gui._DesktopApp(
            root,
            tk=tkinter,
            ttk=ttk,
            filedialog=filedialog,
            messagebox=messagebox,
            scrolledtext=scrolledtext,
            backend="relay",
            relay="127.0.0.1:24872",
            keys="unused",
            phyname="phy0",
            ifname="ldnclient",
            nickname="GUI-Test",
        )
        saves = SaveDialogs(explicit_encrypted, extensionless)
        app.filedialog = saves
        app.messagebox = Messages()
        app.editor_document = PA9EditorDocument.load(source)
        app._editor_populate()
        app.editor_vars["nickname"].set("AFTER")

        app._editor_reload()

        assert app.editor_document is not None
        assert app.editor_document.path == explicit_encrypted
        assert app.editor_document.input_encrypted
        assert app.editor_document.record.nickname == "AFTER"
        assert PA9(source.read_bytes(), encrypted=False).nickname == "BEFORE"
        assert PA9(explicit_encrypted.read_bytes(), encrypted=True).nickname == "AFTER"
        assert saves.calls[0]["defaultextension"] == ".pa9"

        app.editor_vars["nickname"].set("SECOND")
        assert app._editor_save_as()
        defaulted_output = extensionless.with_name(extensionless.name + ".epa9")
        assert app.editor_document.path == defaulted_output
        assert PA9(defaulted_output.read_bytes(), encrypted=True).nickname == "SECOND"
        assert saves.calls[1]["defaultextension"] == ".epa9"
        assert "形式は拡張子で決定" in saves.calls[1]["filetypes"][0][0]
    finally:
        root.destroy()


def test_pa9_editor_mousewheel_routes_windows_and_x11_events() -> None:
    tkinter = pytest.importorskip("tkinter")
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    root = _create_tk_root(tkinter)
    root.withdraw()
    try:
        app = za_gui._DesktopApp(
            root,
            tk=tkinter,
            ttk=ttk,
            filedialog=filedialog,
            messagebox=messagebox,
            scrolledtext=scrolledtext,
            backend="relay",
            relay="127.0.0.1:24872",
            keys="unused",
            phyname="phy0",
            ifname="ldnclient",
            nickname="GUI-Test",
        )
        assert root.bind("<MouseWheel>")
        if str(root.tk.call("tk", "windowingsystem")) == "x11":
            assert root.bind("<Button-4>")
            assert root.bind("<Button-5>")

        canvas = app.scroll_areas[3].canvas
        content = canvas.winfo_children()[0]
        canvas.configure(scrollregion=(0, 0, 100, 1000), yscrollincrement=20)
        canvas.yview_moveto(0.5)
        middle = canvas.yview()[0]

        assert (
            app._mousewheel(
                SimpleNamespace(widget=content, delta=120, num=None)
            )
            == "break"
        )
        assert canvas.yview()[0] < middle

        canvas.yview_moveto(0.5)
        assert (
            app._mousewheel(SimpleNamespace(widget=content, delta=0, num=5))
            == "break"
        )
        assert canvas.yview()[0] > middle

        canvas.yview_moveto(0.5)
        # Outside this editor page, the current router may scroll the outer
        # form, but must never move this unrelated inner canvas.
        app._mousewheel(
            SimpleNamespace(widget=app.editor_open_button, delta=-120, num=None)
        )
        assert canvas.yview()[0] == middle
    finally:
        root.destroy()


def test_pa9_editor_save_as_pins_approved_identity_and_reports_snapshot_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tkinter = pytest.importorskip("tkinter")
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    from zasim.pa9 import PA9
    from zasim.pa9_editor import PA9EditorDocument

    class SaveDialog:
        def __init__(self, value: Path) -> None:
            self.value = str(value)

        def asksaveasfilename(self, **_kwargs) -> str:
            return self.value

    class RaceMessages:
        def __init__(self, target: Path, competing: bytes) -> None:
            self.target = target
            self.competing = competing
            self.errors: list[tuple[str, str]] = []
            self.confirmations = 0

        def askyesno(self, *_args, **_kwargs) -> bool:
            self.confirmations += 1
            self.target.write_bytes(self.competing)
            return True

        def showerror(self, title: str, detail: str, **_kwargs) -> None:
            self.errors.append((title, detail))

    record = PA9()
    record.species_internal = 25
    record.nature = 0
    record.stat_nature = 0
    record.gender = 0
    record.stat_level = 50
    record.stat_hp_max = 100
    record.update_checksum()
    target = tmp_path / "approved.pa9"
    target.write_bytes(record.to_bytes(encrypted=False))
    competing = bytes([0xA5]) * record.size

    root = _create_tk_root(tkinter)
    root.withdraw()
    try:
        app = za_gui._DesktopApp(
            root,
            tk=tkinter,
            ttk=ttk,
            filedialog=filedialog,
            messagebox=messagebox,
            scrolledtext=scrolledtext,
            backend="relay",
            relay="127.0.0.1:24872",
            keys="unused",
            phyname="phy0",
            ifname="ldnclient",
            nickname="GUI-Test",
        )
        app.editor_document = PA9EditorDocument(record)
        app._editor_populate()
        race_messages = RaceMessages(target, competing)
        app.filedialog = SaveDialog(target)
        app.messagebox = race_messages

        assert not app._editor_save_as()
        assert target.read_bytes() == competing
        assert race_messages.confirmations == 1
        assert len(race_messages.errors) == 1
        assert "changed after approval" in race_messages.errors[0][1]

        snapshot_calls: list[tuple[Path, bool]] = []

        def readonly_snapshot(path: object, *, overwrite: bool) -> None:
            snapshot_calls.append((Path(path), overwrite))
            raise PermissionError("PA9 output is read-only")

        class SnapshotErrorMessages:
            def __init__(self) -> None:
                self.errors: list[tuple[str, str]] = []

            def askyesno(self, *_args, **_kwargs) -> bool:
                pytest.fail("confirmation must not run after snapshot failure")

            def showerror(self, title: str, detail: str, **_kwargs) -> None:
                self.errors.append((title, detail))

        readonly_target = tmp_path / "readonly.pa9"
        snapshot_messages = SnapshotErrorMessages()
        app.filedialog = SaveDialog(readonly_target)
        app.messagebox = snapshot_messages
        monkeypatch.setattr(
            za_gui, "snapshot_pa9_output_identity", readonly_snapshot
        )

        assert not app._editor_save_as()
        assert snapshot_calls == [(readonly_target, True)]
        assert len(snapshot_messages.errors) == 1
        assert "read-only" in snapshot_messages.errors[0][1]
        assert not readonly_target.exists()
    finally:
        root.destroy()
