"""Mapped Tk geometry regressions; never connect, inspect keys, or load user data.

Run with a display (e.g. xvfb-run -a python -m pytest tests/test_gui_layout.py).
Both pixel bounds and requested text size are checked: construction alone is not
proof that a control can be seen or operated.
"""
from types import SimpleNamespace

import pytest

from zasim.gui import _DesktopApp


def make_app(monkeypatch, scale=1.3333, language="ja", *, initial_task=0):
    tk = pytest.importorskip("tkinter")
    from tkinter import ttk, filedialog, scrolledtext
    from tkinter import font
    from test_gui import _create_tk_root
    root = _create_tk_root(tk)
    root.tk.call("tk", "scaling", scale)
    # Declare the synthetic DPI fixture's point size explicitly. Windows'
    # native default-font pixel metrics can survive changes to tk scaling;
    # preserve the OS font families, but exercise the requested 10pt scaling.
    for name in ("TkDefaultFont", "TkTextFont", "TkFixedFont"):
        font.nametofont(name, root=root).configure(size=10)
    errors, infos, callbacks = [], [], []
    messages = SimpleNamespace(
        showerror=lambda *a, **kw: errors.append(a),
        showinfo=lambda *a, **kw: infos.append(a),
    )
    def forbidden(*args, **kwargs):
        pytest.fail("layout test must never start a backend or touch user files")
    monkeypatch.setattr("zasim.gui.GuiController.start_trade", forbidden)
    monkeypatch.setattr("zasim.gui.GuiController.start_probe", forbidden)
    root.report_callback_exception = lambda *args: callbacks.append(args)
    app = _DesktopApp(
        root, tk=tk, ttk=ttk, filedialog=filedialog, messagebox=messages,
        scrolledtext=scrolledtext, backend="relay", relay="127.0.0.1:24872",
        keys="unused", phyname="phy0", ifname="ldnclient", nickname="Layout-Test", language=language,
    )
    # Most legacy layout scenarios exercise the connection form explicitly.
    # initial_task=None leaves the real product startup page for usability tests.
    if initial_task is not None:
        app.notebook.select(initial_task)
    return root, app, errors, infos, callbacks


def settle(root):
    root.update_idletasks()
    root.update()
    root.update_idletasks()


def cleanup(root):
    # Tk's after queue is thread-global; don't leak callbacks into the next root.
    try:
        for job in root.tk.call("after", "info"):
            root.after_cancel(job)
        root.destroy()
    except Exception:
        pass  # the test may have exercised the actual exit button


def bounds(widget):
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    return x, y, x + widget.winfo_width(), y + widget.winfo_height()


def visible(widget, viewport):
    left, top, right, bottom = bounds(widget)
    vl, vt, vr, vb = bounds(viewport)
    assert widget.winfo_ismapped(), str(widget)
    assert vl <= left < right <= vr, (str(widget), bounds(widget), bounds(viewport))
    assert vt <= top < bottom <= vb, (str(widget), bounds(widget), bounds(viewport))


def reveal(app, widget):
    for area in reversed(app.scroll_areas):
        area.see(widget)
    settle(app.root)


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


@pytest.mark.parametrize("language", ["ja", "en"])
@pytest.mark.parametrize("scale,size", [
    (1.3333, "1080x820"), (1.3333, "800x600"),
    (2.0, "800x600"), (2.6667, "1080x820"),
    (2.6667, "800x600"), (2.6667, "640x480"),
])
def test_required_controls_fit_and_are_reachable(monkeypatch, scale, size, language):
    root, app, errors, infos, callbacks = make_app(monkeypatch, scale, language)
    try:
        root.geometry(size)
        settle(root)
        assert root.winfo_width() == int(size.split("x")[0])
        assert root.winfo_height() == int(size.split("x")[1])
        controls = (app.relay_radio, app.direct_radio, app.nickname_entry,
                    app.relay_entry, app.keys_entry, app.keys_button,
                    app.radio_combo, app.radio_refresh_button, app.connection_advanced_check,
                    app.phy_entry, app.interface_entry, app.link_code_entry,
                    app.title_version_combo, app.single_interface_combo,
                    app.help_button, app.check_button, app.offer_entry,
                    app.output_entry, app.auto_confirm_check, app.trade_button)
        direct_controls = (app.keys_entry, app.keys_button, app.phy_entry,
                           app.interface_entry, app.single_interface_combo,
                           app.radio_combo, app.radio_refresh_button)
        for widget in controls:
            app.connection_advanced_var.set(widget in (app.nickname_entry, app.title_version_combo,
                                                       app.relay_entry, app.phy_entry,
                                                       app.interface_entry, app.single_interface_combo))
            # Backend-specific settings are intentionally hidden, not clipped.
            if widget in direct_controls:
                if "disabled" in app.direct_radio.state():
                    continue  # Windows cannot select Direct Linux.
                app.direct_radio.invoke()
            else:
                app.relay_radio.invoke()
            settle(root)
            reveal(app, widget)
            visible(widget, root if widget in (app.help_button, app.trade_button) else app.body.canvas)
            assert widget.winfo_width() >= widget.winfo_reqwidth()
            assert widget.winfo_height() >= widget.winfo_reqheight()
            visible(app.stop_button, root)
            visible(app.exit_button, root)
        app.relay_radio.invoke()
        settle(root)
        # Check actual labels, not just widget existence; all active form labels
        # must have their full requested text bounds allocated by grid/pack.
        for widget in descendants(app.body.content):
            if widget.winfo_viewable() and widget.winfo_class() == "TLabel":
                assert widget.winfo_width() >= widget.winfo_reqwidth(), widget.cget("text")
                assert widget.winfo_height() >= widget.winfo_reqheight(), widget.cget("text")
        # Harmless edits plus backend selection exercise the actual widgets;
        # selecting Direct only changes field state and must not start anything.
        app.nickname_entry.delete(0, "end")
        app.nickname_entry.insert(0, "LAYOUT")
        assert app.nickname_var.get() == "LAYOUT"
        app.link_code_entry.insert(0, "12345678")
        assert app.link_code_var.get() == "12345678"
        assert app.link_code_entry.cget("show") == "*"
        app.link_code_entry.delete(0, "end")
        if "disabled" not in app.direct_radio.state():
            app.direct_radio.invoke()
            assert app.backend_var.get() == "direct"
            assert "disabled" not in app.keys_entry.state()
            assert "disabled" not in app.interface_entry.state()
            assert "disabled" not in app.single_interface_combo.state()
            app.relay_radio.invoke()
        assert "disabled" in app.keys_entry.state()
        assert "disabled" not in app.relay_entry.state()
        app.status_var.set("long status " * 200)
        settle(root)
        visible(app.stop_button, root)
        visible(app.exit_button, root)
        assert app.auto_confirm_var.get() is False
        assert "disabled" in app.stop_button.state()
        reveal(app, app.help_button)
        app.help_button.invoke()
        settle(root)
        assert "2.0.2" in app.help_text.get("1.0", "end")
        visible(app.help_close_button, app.help_window)
        assert app.help_window.winfo_width() <= root.winfo_width()
        app.help_text.yview_moveto(1)
        app.help_close_button.invoke()
        # Exercise the real start callback, with blank required fields. It must
        # report validation, not enter the forbidden controller/backend.
        reveal(app, app.trade_button)
        app.trade_button.invoke()
        assert errors and ("開始できません" if language == "ja" else "Could not start") in errors[-1][0]
        assert not callbacks
    finally:
        cleanup(root)


def test_reflow_on_shrink_grow_and_keyboard_focus(monkeypatch):
    root, app, _, _, callbacks = make_app(monkeypatch, 2.6667)
    try:
        for size in ("1200x900", "640x480", "1080x820", "800x600"):
            root.geometry(size)
            settle(root)
            # Natural widths from an earlier wide layout must not prevent shrink.
            assert app.body.content.winfo_reqwidth() <= root.winfo_width()
            app.body.canvas.yview_moveto(0)
            app.trade_button.event_generate("<FocusIn>")
            settle(root)
            visible(app.trade_button, root)
            app.link_code_entry.event_generate("<FocusIn>")
            settle(root)
            visible(app.link_code_entry, app.body.canvas)
        assert not callbacks
    finally:
        cleanup(root)


def test_wheel_routing_editor_horizontal_and_all_tab_actions(monkeypatch):
    root, app, _, _, callbacks = make_app(monkeypatch, 2.6667)
    try:
        root.geometry("800x600")
        settle(root)
        app.body.canvas.yview_moveto(0)
        app.link_code_entry.event_generate("<Button-5>") if root.tk.call("tk", "windowingsystem") == "x11" else app.link_code_entry.event_generate("<MouseWheel>", delta=-120)
        settle(root)
        assert app.body.canvas.yview()[0] > 0
        # Native text/combobox scroll is not duplicated in the outer viewport.
        before = app.body.canvas.yview()
        assert app._mousewheel(SimpleNamespace(widget=app.title_version_combo, delta=-120)) is None
        assert app.body.canvas.yview() == before
        for index, button in ((1, app.editor_empty_open_button), (2, app.probe_button),
                              (3, app.analyze_button)):
            app.notebook.select(index)
            settle(root)
            reveal(app, button)
            visible(button, root if button in (app.probe_button, app.editor_empty_open_button) else app.body.canvas)
        from zasim.pa9_creation import CreateRequest
        from zasim.pa9_editor import PA9EditorDocument
        app.editor_document = PA9EditorDocument.create(CreateRequest(152, "TEST", 1, 2, language=2, seed=11))
        app._editor_populate()
        app.notebook.select(app.editor_tab)
        # Responsive fields need not overflow at 800px. The real moves table at
        # an intentionally narrow width must still have a horizontal fallback.
        root.geometry("360x900")
        app.editor_notebook.select(1)
        settle(root)
        moves = app.scroll_areas[2]
        assert moves.canvas.xview()[1] < 1
        first = moves.canvas.xview()
        assert app._mousewheel(SimpleNamespace(widget=moves.content, num=5, state=1)) == "break"
        assert moves.canvas.xview() != first
        root.geometry("800x600")
        app.editor_notebook.select(0)
        settle(root)
        editor = app.scroll_areas[1]
        # Nested wheel events scroll the inner editor first, not both viewports.
        app.body.canvas.yview_moveto(0.5)
        before = app.body.canvas.yview()
        editor.canvas.yview_moveto(0)
        app._mousewheel(SimpleNamespace(widget=editor.content, num=5, state=0))
        assert editor.canvas.yview()[0] > 0
        assert app.body.canvas.yview() == before
        app.notebook.select(4)
        settle(root)
        visible(app.exit_button, root)
        assert not callbacks
    finally:
        cleanup(root)


def test_stop_and_exit_callbacks_without_a_live_job(monkeypatch):
    root, app, _, _, callbacks = make_app(monkeypatch)
    try:
        root.geometry("640x480")
        settle(root)
        calls = []
        monkeypatch.setattr(app.controller, "cancel", lambda: calls.append("cancel") or True)
        app._set_live_busy(True, "synthetic busy state — no worker")
        app.stop_button.invoke()
        assert calls == ["cancel"]
        assert "disabled" in app.stop_button.state()
        app._set_live_busy(False)
        assert not callbacks
        closed = []
        root.bind("<Destroy>", lambda event: closed.append(event.widget is root), add="+")
        app.exit_button.invoke()
        assert any(closed)
    finally:
        cleanup(root)


def test_default_geometry_clamps_to_small_screen(monkeypatch):
    import tkinter as tk
    monkeypatch.setattr(tk.Tk, "winfo_screenwidth", lambda _: 640)
    monkeypatch.setattr(tk.Tk, "winfo_screenheight", lambda _: 480)
    root, app, _, _, callbacks = make_app(monkeypatch)
    try:
        settle(root)
        assert root.winfo_width() <= 640 and root.winfo_height() <= 480
        assert root.minsize()[0] <= 360 and root.minsize()[1] <= 240
        visible(app.stop_button, root)
        visible(app.exit_button, root)
        assert not callbacks
    finally:
        cleanup(root)


@pytest.mark.parametrize("language", ["ja", "en"])
@pytest.mark.parametrize("scale,size", [(1.3333,"1080x820"),(2.0,"800x600"),(2.6667,"640x480")])
def test_polished_navigation_and_start_remain_visible_when_form_scrolls(monkeypatch, language, scale, size):
    root,app,errors,infos,callbacks=make_app(monkeypatch,scale,language)
    try:
        root.geometry(size)
        for index in range(5):
            app.task_buttons[index].invoke();settle(root)
            assert app.notebook.index(app.notebook.select())==index
            assert app.task_var.get()==str(index)
            if index == 1:
                from zasim.pa9_creation import CreateRequest
                from zasim.pa9_editor import PA9EditorDocument
                app.editor_document=PA9EditorDocument.create(CreateRequest(152,'Test',1,2,language=2,seed=11))
                app._editor_populate();settle(root);app.body.canvas.yview_moveto(0)
                visible(app.editor_save_as_button,root)
            for fraction in (0.0,1.0):
                app.body.canvas.yview_moveto(fraction);settle(root)
                navigation = (app.task_combo,) if app._compact_navigation else tuple(app.task_buttons)
                for control in (*navigation,app.language_combo,app.header_help_button,app.stop_button,app.exit_button):
                    visible(control,root)
                if index in (0,1,2):
                    action=app.trade_button if index==0 else app.editor_save_button if index==1 else app.probe_button
                    visible(action,root)
                    assert not app.body.contains(action)
                else:
                    assert not app.task_action_bar.winfo_ismapped()
        assert not callbacks and not errors
    finally:cleanup(root)


@pytest.mark.parametrize("language", ["ja", "en"])
def test_polished_empty_editor_and_save_hierarchy_preserve_actual_document(monkeypatch,tmp_path,language):
    from zasim.pa9_creation import CreateRequest
    from zasim.pa9_editor import PA9EditorDocument
    root,app,errors,infos,callbacks=make_app(monkeypatch,language=language)
    try:
        app.task_buttons[1].invoke();settle(root)
        assert app.editor_empty.winfo_viewable() and not app.editor_notebook.winfo_ismapped()
        assert app.editor_new_button.cget('style')=='Accent.TButton'
        app.editor_document=PA9EditorDocument.create(CreateRequest(152,'Test',1,2,language=2,seed=11))
        app._editor_populate();settle(root)
        assert app.editor_notebook.winfo_viewable() and not app.editor_empty.winfo_ismapped()
        assert app.editor_save_button.cget('style')=='Accent.TButton'
        assert 'disabled' not in app.editor_save_button.state()
        original=app.editor_document.record.data
        app.editor_vars['nickname'].set('Pending')
        assert app.editor_document.record.data==original
        assert '*' in app.task_buttons[1].cget('text')
        output=tmp_path/'saved.pa9'
        monkeypatch.setattr(app.filedialog,'asksaveasfilename',lambda **_:str(output))
        app.editor_save_as_button.invoke()
        assert output.read_bytes()==app.editor_document.record.data
        assert app.editor_document.record.nickname=='Pending'
        assert app.editor_save_button.cget('style')=='Accent.TButton'
        assert app.editor_save_as_button.cget('style')=='TButton'
        assert '*' not in app.task_buttons[1].cget('text')
        assert not errors and not callbacks
    finally:cleanup(root)


def test_polished_task_shortcuts_do_not_edit_fields_or_cross_modal_grab(monkeypatch):
    root,app,errors,infos,callbacks=make_app(monkeypatch)
    try:
        app.link_code_var.set('12345678');root.focus_force();settle(root)
        for index in range(5):
            root.event_generate(f'<Alt-Key-{index+1}>');settle(root)
            assert app.notebook.index(app.notebook.select())==index
        assert app.link_code_var.get()=='12345678'
        app._editor_shortcut(app._editor_new);settle(root)
        before=app.notebook.select()
        assert app._task_shortcut(4)=='break'
        assert app.notebook.select()==before
        app.editor_new_dialog.cancel_button.invoke()
        assert not callbacks
    finally:cleanup(root)


def test_polished_result_is_readonly_and_keeps_horizontal_access(monkeypatch):
    root,app,errors,infos,callbacks=make_app(monkeypatch)
    try:
        app.task_buttons[3].invoke();settle(root)
        app._show_result({'synthetic':'x'*1000})
        text=app.result_text.get('1.0','end-1c')
        assert app.result_text.cget('state')=='disabled'
        app.result_text.insert('1.0','should not appear')
        assert app.result_text.get('1.0','end-1c')==text
        settle(root)
        assert app.result_text.xview()[1]<1
        app.result_text.xview_moveto(1)
        assert app.result_text.xview()[0]>0
        assert app.change_language('en');settle(root)
        assert app.result_text.get('1.0','end-1c')==text
        assert app.result_text.cget('state')=='disabled'
        assert not callbacks
    finally:cleanup(root)


@pytest.mark.parametrize('scale',[1.3333,2.0,2.6667])
def test_nested_responsive_breakpoints_converge_after_document_and_resize(monkeypatch,scale):
    from zasim.pa9_creation import CreateRequest
    from zasim.pa9_editor import PA9EditorDocument
    root,app,errors,infos,callbacks=make_app(monkeypatch,scale,language='en')
    calls=[];original=app._reflow_layout
    def counted():
        calls.append(1)
        if len(calls)>60:
            # Bounded negative control: fail below instead of hanging pytest in
            # update_idletasks. This guard is never reached by passing product.
            app._layout_pending=True
            return
        original()
    monkeypatch.setattr(app,'_reflow_layout',counted)
    try:
        # Reproduce creation/loading after the empty editor is already narrow;
        # loading at the initial wide size and shrinking is a different path.
        root.geometry('800x600');app.notebook.select(app.editor_tab);settle(root)
        calls.clear()
        app.editor_document=PA9EditorDocument.create(CreateRequest(152,'Test',1,2,language=2,seed=11))
        app._editor_populate();settle(root)
        assert len(calls)<=60,(scale,'load-at-800x600',len(calls))
        assert not app._layout_pending
        for size in ('800x600','1080x820','640x480','900x700','800x600'):
            calls.clear();root.geometry(size);settle(root)
            assert len(calls)<=60,(scale,size,len(calls))
            assert not app._layout_pending
        assert not callbacks
    finally:cleanup(root)
