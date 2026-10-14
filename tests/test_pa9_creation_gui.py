"""New/unsaved GUI lifecycle on our own isolated mapped Tk window."""

import pytest

from test_gui_layout import make_app, settle, cleanup, visible, reveal
from zasim.pa9 import PA9
from zasim.pa9_creation import CreateRequest
from zasim.pa9_editor import PA9EditorDocument


@pytest.mark.parametrize("scale,size", [(1.3333,"800x600"),(2.6667,"640x480")])
def test_new_form_operates_and_can_cancel_without_any_files(monkeypatch,scale,size):
    root, app, errors, _, callbacks = make_app(monkeypatch,scale)
    try:
        root.geometry(size)
        app.notebook.select(app.editor_tab)
        settle(root)
        # First-use actions now live directly in the editor workspace, outside
        # the connection/task scroller and visible even in compact mode.
        visible(app.editor_empty_new_button,root)
        app.editor_empty_new_button.invoke()
        settle(root)
        d = app.editor_new_dialog
        assert d.window.winfo_width() <= root.winfo_width()
        assert d.window.winfo_height() <= root.winfo_height()
        visible(d.create_button,d.window)
        visible(d.cancel_button,d.window)
        # Optional fields are deliberately disclosed rather than filling the
        # initial form; after disclosure every original field remains reachable.
        assert not d.optional_fields.winfo_ismapped()
        d.optional_toggle.invoke()
        settle(root)
        for name, widget in d.widgets.items():
            if name in ("met_location", "met_level", "ball"):
                assert not widget.winfo_ismapped()  # fixed by the starter preset
                continue
            d.area.see(widget)
            settle(root)
            visible(widget,d.area.canvas)
        d.create_button.invoke()  # blank trainer/IDs fail and leave editor untouched
        assert errors and app.editor_document is None
        d.vars["encounter"].set("custom")
        assert "disabled" not in d.widgets["met_level"].state()
        settle(root)
        assert all(d.widgets[name].winfo_ismapped() for name in ("met_location", "met_level", "ball"))
        assert len(d.widgets["species"]["values"]) == 490
        d.vars["encounter"].set("starter")
        assert "disabled" in d.widgets["met_level"].state()
        assert len(d.widgets["species"]["values"]) == 6
        d.cancel_button.invoke()
        assert app.editor_document is None and not callbacks
    finally:
        cleanup(root)


def test_create_apply_recalculate_and_save_exact_party(monkeypatch,tmp_path):
    root, app, errors, _, callbacks = make_app(monkeypatch)
    try:
        root.geometry("800x600")
        app.notebook.select(app.editor_tab)
        settle(root)
        app.editor_new_button.invoke()
        d = app.editor_new_dialog
        for name,value in {"ot_name":"テスト","tid":"12345","sid":"54321","met_date":"2026-10-05"}.items():
            d.vars[name].set(value)
        d.create_button.invoke()
        assert app.editor_document is not None and app.editor_document.dirty
        assert app.editor_document.path is None and app.editor_document.auto_recalculate
        assert "disabled" in app.editor_reload_button.state()
        assert all("disabled" in w.state() for w in app.editor_calculated_widgets)
        app.editor_vars["stat_level"].set("50")
        app.editor_vars["iv_hp"].set("31")
        app.editor_vars["ev_hp"].set("252")
        app.editor_apply_button.invoke()
        assert app.editor_document.record.stat_level == 50
        assert app.editor_document.record.stat_hp_max == 152
        assert not app.editor_document.derived_stats_may_be_stale
        output = tmp_path/"from-gui.epa9"
        monkeypatch.setattr(app.filedialog,"asksaveasfilename",lambda **kw: str(output))
        app.editor_save_as_button.invoke()
        assert output.stat().st_size == 344
        assert PA9(output.read_bytes(),encrypted=True).data == app.editor_document.record.data
        assert not app._editor_is_dirty()
        assert "disabled" not in app.editor_reload_button.state()
        assert not errors and not callbacks
    finally:
        cleanup(root)


def test_reject_replacement_of_unsaved_document_and_cancel_leaves_it(monkeypatch):
    root, app, errors, _, callbacks = make_app(monkeypatch)
    try:
        app.editor_document = PA9EditorDocument.create(CreateRequest(152,"テスト",1,2,seed=1))
        app._editor_populate()
        original = app.editor_document
        original_bytes = original.record.data
        app.messagebox.askyesnocancel = lambda *a,**kw: None
        app._editor_new()
        d = app.editor_new_dialog
        for name,value in {"ot_name":"テスト","tid":"3","sid":"4"}.items():
            d.vars[name].set(value)
        d.submit()
        assert d.window.winfo_exists()
        assert app.editor_document is original and original.record.data == original_bytes
        d.cancel_button.invoke()
        assert app.editor_document is original and app._editor_is_dirty()
        assert not errors and not callbacks
    finally:
        cleanup(root)


def test_gui_stored_conversion_uses_real_calculation(monkeypatch,tmp_path):
    root, app, errors, _, callbacks = make_app(monkeypatch)
    try:
        source = PA9EditorDocument.create(CreateRequest(152,"テスト",1,2,seed=1)).record
        app.editor_document = PA9EditorDocument(PA9(source.to_bytes(party=False),encrypted=False))
        app._editor_populate()
        before = app.editor_document.record.data
        app.messagebox.askyesno = lambda *a,**kw: False
        app.editor_recalculate_button.invoke()
        assert app.editor_document.record.data == before
        app.messagebox.askyesno = lambda *a,**kw: True
        app.editor_recalculate_button.invoke()
        assert app.editor_document.record.is_party
        assert app.editor_document.record.data == source.data
        assert app.editor_document.dirty
        assert not errors and not callbacks
    finally:
        cleanup(root)


@pytest.mark.parametrize('language',['ja','en'])
@pytest.mark.parametrize('query',['152','CHIKORITA','チコリータ','not-a-species-xyz'])
def test_species_filter_is_local_and_never_silently_changes_selection(monkeypatch,language,query):
    root,app,errors,_,callbacks=make_app(monkeypatch,language=language)
    try:
        app._editor_new();settle(root);d=app.editor_new_dialog
        original=d.vars['species'].get()
        d.search_var.set(query);settle(root)
        values=d.widgets['species']['values']
        assert d.vars['species'].get()==original
        if query=='not-a-species-xyz':assert len(values)==0
        else:assert any(label.startswith('152:0 ') for label in values)
        assert app.editor_document is None
        d.search_clear_button.invoke()
        assert len(d.widgets['species']['values'])==6 and d.vars['species'].get()==original
        d.vars['encounter'].set('custom')
        assert len(d.widgets['species']['values'])==490
        d.search_var.set('Pikachu')
        label=next(label for label in d.widgets['species']['values'] if label.startswith('25:0 '))
        d.widgets['species'].set(label)
        for name,value in {'ot_name':'Test','tid':'1','sid':'2','language':'2','met_level':'5','met_location':'1'}.items():
            d.vars[name].set(value)
        assert d.request().species==25
        d.search_var.set('no match')
        assert d.request().species==25 and d.vars['species'].get()==label
        d.cancel_button.invoke()
        assert not d.search_var.trace_info() and not d.vars['encounter'].trace_info()
        assert not callbacks and not errors
    finally:cleanup(root)


@pytest.mark.parametrize('language',['ja','en'])
def test_new_dialog_optional_disclosure_preserves_request_and_foreign_search_trace(monkeypatch,language):
    root,app,errors,_,callbacks=make_app(monkeypatch,language=language)
    try:
        app._editor_new();settle(root);d=app.editor_new_dialog
        for name,value in {'ot_name':'Test','tid':'1','sid':'2','language':'2','nickname':'Demo','met_date':'2026-10-12'}.items():
            d.vars[name].set(value)
        expected=d.request()
        for _ in range(4):
            d.optional_toggle.invoke();settle(root)
            assert d.request()==expected
        calls=[]
        foreign=d.search_var.trace_add('write',lambda *_:calls.append('search'))
        d.search_var.set('152');d.cancel_button.invoke()
        assert d.search_var.trace_info()==[(('write',),foreign)]
        d.search_var.set('after destroy')
        assert calls==['search','search']
        d.search_var.trace_remove('write',foreign)
        assert not callbacks and not errors
    finally:cleanup(root)
