"""Task-level UI contracts. Synthetic records, owned Tk, no device operations."""

import gc
import weakref

import pytest
from test_gui_layout import make_app, settle, cleanup, visible, reveal
from zasim.pa9_creation import CreateRequest
from zasim.pa9_editor import PA9EditorDocument
from zasim import ui_catalog, i18n


def document(app):
    app.editor_document = PA9EditorDocument.create(
        CreateRequest(152, "Test", 1, 2, language=2, seed=7)
    )
    app._editor_populate()
    app.notebook.select(app.editor_tab)
    settle(app.root)
    return app.editor_document


@pytest.mark.parametrize(
    "kind,query,value",
    [
        ("species", "ちこりーた", 152),
        ("species", "CHIKORITA", 152),
        ("species", "１５２", 152),
        ("moves", "Thunderbolt", 85),
        ("moves", "１０まんボルト", 85),
        ("items", "poke ball", 4),
        ("items", "モンスターボール", 4),
        ("abilities", "Overgrow", 65),
        ("abilities", "しんりょく", 65),
    ],
)
def test_offline_search_uses_bilingual_names_and_explicit_ids(kind, query, value):
    matches = ui_catalog.search(kind, query, "en")
    assert any(row.value == value for row in matches)
    assert not ui_catalog.search(kind, "no-such-label-00000000", "ja")


def test_catalog_language_switch_and_unknown_values_do_not_change_ids():
    old = i18n.get_language()
    try:
        i18n.set_language("ja")
        ja = ui_catalog.choices("moves")
        i18n.set_language("en")
        en = ui_catalog.choices("moves")
        assert ja[85].label == "１０まんボルト" and en[85].label == "Thunderbolt"
        assert ui_catalog.display_value("moves", 65535) == "65535"
        assert [r.value for r in ja] == [r.value for r in en]
        # Species selectors map National display numbers onto the existing
        # internal PA9 IDs; do not assume those two spaces are identical.
        rows = ui_catalog.choices("species", "en")
        assert any(r.value != r.number for r in rows)
    finally:
        i18n.set_language(old)


@pytest.mark.parametrize("language", ["ja", "en"])
@pytest.mark.parametrize(
    "scale,size", [(1.3333, "1080x820"), (2.0, "800x600"), (2.6667, "640x480")]
)
def test_startup_and_fixed_save_allow_new_document_without_an_apply_step(
    monkeypatch, tmp_path, language, scale, size
):
    root, app, errors, _, callbacks = make_app(
        monkeypatch, scale, language, initial_task=None
    )
    try:
        root.geometry(size)
        settle(root)
        assert app.notebook.select() == str(app.editor_tab)
        assert not app.connection_panel.winfo_ismapped()
        assert app.editor_empty.winfo_viewable()
        doc = document(app)
        assert "disabled" not in app.editor_save_button.state()
        assert (
            not app.editor_advanced_var.get()
            and not app.editor_technical_frame.winfo_ismapped()
        )
        assert not app.editor_status_label.winfo_ismapped()
        assert ("Chikorita" if language == "en" else "チコリータ") in app.editor_vars[
            "species_internal"
        ].get()
        app.editor_vars["nickname"].set("Pending")
        original = doc.record.data
        for fraction in (0.0, 1.0):
            app.body.canvas.yview_moveto(fraction)
            settle(root)
            visible(app.editor_save_button, root)
            visible(app.editor_save_as_button, root)
            assert not app.body.contains(app.editor_save_button)
        target = tmp_path / "named.pa9"
        monkeypatch.setattr(
            app.filedialog, "asksaveasfilename", lambda **_: str(target)
        )
        app.editor_save_button.invoke()
        assert (
            target.read_bytes() == doc.record.data and doc.record.nickname == "Pending"
        )
        assert doc.record.data != original and not app._editor_is_dirty()
        assert not errors and not callbacks
    finally:
        cleanup(root)


@pytest.mark.parametrize(
    "field,query,value",
    [
        ("move1", "Thunderbolt", 85),
        ("held_item", "poke ball", 4),
        ("ability", "Overgrow", 65),
        ("species_internal", "Chikorita", 152),
    ],
)
def test_real_picker_requires_selection_and_preserves_unapplied_record(
    monkeypatch, field, query, value
):
    root, app, errors, _, callbacks = make_app(monkeypatch, initial_task=None)
    try:
        doc = document(app)
        before = doc.record.data
        before_fields = {name: var.get() for name, var in app.editor_vars.items()}
        app._editor_pick_catalog(field)
        picker = app.catalog_dialog
        settle(root)
        assert picker.window.winfo_viewable() and root.grab_current() is picker.window
        picker.query.set("does-not-exist")
        settle(root)
        assert not picker.rows and "disabled" in picker.choose.state()
        picker.select()
        assert picker.window.winfo_exists() and doc.record.data == before
        picker.query.set(query)
        settle(root)
        assert "disabled" in picker.choose.state()  # no implicit first result
        row = next(i for i, r in enumerate(picker.rows) if r.value == value)
        picker.tree.selection_set(str(row))
        settle(root)
        picker.choose.invoke()
        assert not picker.window.winfo_exists()
        assert int(app.editor_vars[field].get().split(":", 1)[0]) == value
        assert doc.record.data == before
        assert app.editor_form_dirty == (
            before_fields != {name: var.get() for name, var in app.editor_vars.items()}
        )
        assert app.change_language("en")
        assert app.change_language("ja")
        assert int(app.editor_vars[field].get().split(":", 1)[0]) == value
        assert doc.record.data == before
        assert not errors and not callbacks
    finally:
        cleanup(root)


@pytest.mark.parametrize(
    "field,value,page,advanced",
    [
        ("pid", "not-a-number", 0, True),
        ("ev_hp", "999", 2, False),
        ("move1", "bad move", 1, False),
    ],
)
def test_bad_input_selects_the_right_page_and_field_without_applying(
    monkeypatch, field, value, page, advanced
):
    root, app, errors, _, callbacks = make_app(monkeypatch, initial_task=None)
    try:
        root.geometry("800x600")
        doc = document(app)
        before = doc.record.data
        root.focus_force()
        app.editor_vars[field].set(value)
        app.editor_apply_button.invoke()
        settle(root)
        widget = app.editor_field_widgets[field]
        assert errors and doc.record.data == before
        assert app.editor_notebook.index(app.editor_notebook.select()) == page
        assert app.editor_advanced_var.get() is advanced
        assert "invalid" in widget.state()
        assert root.focus_get() is widget
        for area in app.scroll_areas:
            if area.contains(widget):
                visible(widget, area.canvas)
        app.editor_vars[field].set("0")
        assert "invalid" not in widget.state()
        assert not callbacks
    finally:
        cleanup(root)


def test_new_dialog_generates_ids_only_on_request_and_preserves_request_semantics(
    monkeypatch,
):
    root, app, errors, _, callbacks = make_app(monkeypatch, initial_task=None)
    try:
        app.editor_new_button.invoke()
        d = app.editor_new_dialog
        settle(root)
        assert d.vars["tid"].get() == "" and d.vars["sid"].get() == ""
        d.create_button.invoke()
        settle(root)
        assert errors and str(
            d.window.tk.call("focus", "-lastfor", str(d.window))
        ) == str(d.widgets["ot_name"])
        d.vars["ot_name"].set("Test")
        d.generate_ids_button.invoke()
        assert all(0 <= int(d.vars[n].get()) <= 65535 for n in ("tid", "sid"))
        request = d.request()
        assert (
            request.encounter == "starter"
            and request.language == 1
            and request.ot_gender == 0
        )
        assert request.tid == int(d.vars["tid"].get()) and request.sid == int(
            d.vars["sid"].get()
        )
        d.create_button.invoke()
        assert app.editor_document is not None and app.editor_document.path is None
        assert not callbacks
    finally:
        cleanup(root)


def test_picker_cancel_stale_document_and_resource_failure_do_not_edit(monkeypatch):
    root, app, errors, _, callbacks = make_app(monkeypatch, initial_task=None)
    try:
        doc = document(app)
        before = doc.record.data
        app._editor_pick_catalog("move1")
        d = app.catalog_dialog
        settle(root)
        d.query.set("Thunderbolt")
        d.cancel.invoke()
        assert doc.record.data == before and not app.editor_form_dirty
        app._editor_pick_catalog("move1")
        d = app.catalog_dialog
        settle(root)
        replacement = document(app)
        assert d.accept(ui_catalog.search("moves", "85", "en")[0]) is False
        assert not app.editor_form_dirty
        d.cancel.invoke()
        monkeypatch.setattr(
            "zasim.catalog_picker.search",
            lambda *_: (_ for _ in ()).throw(ValueError("catalog missing")),
        )
        app._editor_pick_catalog("move1")
        assert errors and root.grab_current() is None
        assert app.editor_document is replacement and not callbacks
    finally:
        cleanup(root)


@pytest.mark.parametrize("finish", ["cancel", "select", "root"])
def test_picker_owned_trace_and_application_are_released(monkeypatch, finish):
    root, app, errors, _, callbacks = make_app(monkeypatch, initial_task=None)
    document(app)
    app._editor_pick_catalog("move1")
    d = app.catalog_dialog
    settle(root)
    references = (weakref.ref(root), weakref.ref(app), weakref.ref(d))
    if finish == "cancel":
        d.cancel.invoke()
    elif finish == "select":
        d.query.set("85")
        d._first()
        d.select()
    if finish != "root":
        assert not d.query.trace_info()
    root.destroy()
    assert d._trace is None and not callbacks
    del d, app, root
    gc.collect()
    assert all(ref() is None for ref in references)


@pytest.mark.parametrize("language", ["ja", "en"])
@pytest.mark.parametrize("scale", [1.3333, 2.0, 2.6667])
def test_picker_rows_fit_native_font_and_keyboard_selection(
    monkeypatch, language, scale
):
    from tkinter import font

    root, app, errors, _, callbacks = make_app(
        monkeypatch, scale, language, initial_task=None
    )
    try:
        root.geometry("640x480")
        doc = document(app)
        before = doc.record.data
        app._editor_pick_catalog("move1")
        d = app.catalog_dialog
        d.query.set("85")
        settle(root)
        assert len(d.rows) == 1
        row = d.tree.bbox("0")
        assert (
            row
            and row[3]
            >= font.nametofont("TkDefaultFont", root=root).metrics("linespace") + 8
        )
        visible(d.entry, d.window)
        visible(d.choose, d.window)
        visible(d.cancel, d.window)
        assert "disabled" in d.choose.state()
        d._first()
        settle(root)
        d.select()
        assert not d.window.winfo_exists() and doc.record.data == before
        assert app.editor_vars["move1"].get().startswith("85: ")
        assert not errors and not callbacks
    finally:
        cleanup(root)


@pytest.mark.parametrize("scale", [1.3333, 2.0, 2.6667])
def test_compact_task_selection_and_advanced_menu_preserve_edits(monkeypatch, scale):
    root, app, errors, _, callbacks = make_app(monkeypatch, scale, initial_task=None)
    try:
        root.geometry("640x480")
        document(app)
        app.editor_vars["nickname"].set("Pending")
        settle(root)
        if scale >= 2.0:
            assert app._compact_navigation, (
                "high-DPI navigation must not consume two rows"
            )
        if app._compact_navigation:
            visible(app.task_combo, root)
            for index in (4, 0, 2, 3, 1):
                app.task_combo.current(index)
                app.task_combo.event_generate("<<ComboboxSelected>>")
                settle(root)
                assert app.notebook.index(app.notebook.select()) == index
        app.notebook.select(app.editor_tab)
        settle(root)
        assert not app.editor_actions.winfo_ismapped()
        app.editor_menu.invoke(5)
        settle(root)  # actual menu checkbutton
        assert (
            app.editor_advanced_var.get()
            and app.editor_technical_frame.winfo_viewable()
        )
        if app._compact_editor:
            # The compact menu exposes the same commands without a second
            # action row stealing space from the scrolling fields.
            assert not app.editor_actions.winfo_viewable()
            visible(app.editor_file_button, root)
            assert app.editor_menu.entrycget(0, "state") == "normal"
        else:
            assert app.editor_actions.winfo_viewable()
        app.editor_menu.invoke(5)
        settle(root)
        assert not app.editor_advanced_var.get()
        assert app.editor_vars["nickname"].get() == "Pending" and app.editor_form_dirty
        assert not errors and not callbacks
    finally:
        cleanup(root)


def test_mismatched_display_name_is_not_silently_reinterpreted_or_translated(
    monkeypatch,
):
    root, app, errors, _, callbacks = make_app(monkeypatch, initial_task=None)
    try:
        doc = document(app)
        original = doc.record.data
        app.editor_vars["move1"].set("85: Tackle")
        app.editor_apply_button.invoke()
        assert errors and doc.record.data == original
        assert app.change_language("en")
        assert app.editor_vars["move1"].get() == "85: Tackle"
        app.editor_vars["move1"].set("85")
        assert app._editor_collect_changes()["move1"] == 85
        assert doc.record.data == original and not callbacks
    finally:
        cleanup(root)


@pytest.mark.parametrize("category", ["internal-id", "form"])
def test_species_picker_maps_displayed_national_number_and_form_to_model_fields(
    monkeypatch, category
):
    from zasim.pa9_creation import species_catalog

    expected = next(
        p
        for p in species_catalog()
        if (
            p["species"] != p["internal_id"]
            if category == "internal-id"
            else p["form"] != 0
        )
    )
    root, app, errors, _, callbacks = make_app(monkeypatch, initial_task=None)
    try:
        doc = document(app)
        before = doc.record.data
        app._editor_pick_catalog("species_internal")
        d = app.catalog_dialog
        d.query.set(str(expected["species"]))
        settle(root)
        index = next(
            i
            for i, row in enumerate(d.rows)
            if row.number == expected["species"] and row.form == expected["form"]
        )
        d.tree.selection_set(str(index))
        settle(root)
        d.choose.invoke()
        assert not d.window.winfo_exists()
        pending = app._editor_collect_changes()
        assert pending["species_internal"] == expected["internal_id"]
        assert pending["form"] == expected["form"]
        assert doc.record.data == before and not errors and not callbacks
    finally:
        cleanup(root)


def test_save_as_shortcut_obeys_modal_and_closing_guards(monkeypatch):
    root, app, errors, _, callbacks = make_app(monkeypatch, initial_task=None)
    calls = []
    monkeypatch.setattr(app, "_editor_save_as", lambda: calls.append(True))
    try:
        document(app)
        root.focus_force()
        settle(root)
        root.event_generate("<Control-Shift-S>")
        settle(root)
        assert calls == [True]
        app._editor_pick_catalog("move1")
        settle(root)
        root.event_generate("<Control-Shift-S>")
        settle(root)
        assert calls == [True]
        app.catalog_dialog.cancel.invoke()
        app.close_when_idle = True
        root.event_generate("<Control-Shift-S>")
        settle(root)
        assert calls == [True] and not callbacks
    finally:
        cleanup(root)


@pytest.mark.parametrize("language", ["ja", "en"])
def test_new_document_reveals_an_editable_field_at_high_dpi(monkeypatch, language):
    root, app, errors, _, callbacks = make_app(
        monkeypatch, 2.6667, language, initial_task=None
    )
    try:
        root.geometry("640x480")
        settle(root)
        app.editor_new_button.invoke()
        d = app.editor_new_dialog
        for n, v in {"ot_name": "Test", "tid": "1", "sid": "2"}.items():
            d.vars[n].set(v)
        d.create_button.invoke()
        settle(root)
        widget = app.editor_field_widgets["nickname"]
        for area in app.scroll_areas:
            if area.contains(widget):
                visible(widget, area.canvas)
        visible(app.editor_save_button, root)
        assert not errors and not callbacks
        before = app.editor_document.record.data
        app.notebook.select(app.trade_tab)
        settle(root)
        app._editor_reveal_new(app.editor_document)
        assert (
            app.notebook.select() == str(app.trade_tab)
            and app.editor_document.record.data == before
        )
    finally:
        cleanup(root)


@pytest.mark.parametrize("language", ["ja", "en"])
def test_nested_keyboard_focus_reveals_whole_control_after_editor_tab_switch(
    monkeypatch, language
):
    root, app, errors, _, callbacks = make_app(
        monkeypatch, 2.6667, language, initial_task=None
    )
    try:
        root.geometry("640x480")
        document(app)
        settle(root)
        for name in (
            "species_internal",
            "move1",
            "held_item",
            "ability",
            "move4",
            "nickname",
        ):
            entry = app.editor_field_widgets[name]
            widget = next(
                (
                    w
                    for w in entry.master.winfo_children()
                    if w.winfo_class() == "TButton"
                ),
                entry,
            )
            for page in app.editor_notebook.tabs():
                if str(widget).startswith(page + "."):
                    app.editor_notebook.select(page)
            settle(root)
            widget.focus_force()
            settle(root)
            assert root.focus_get() is widget
            for area in app.scroll_areas:
                if area.contains(widget):
                    visible(widget, area.canvas)
        assert not errors and not callbacks
    finally:
        cleanup(root)


@pytest.mark.parametrize("language", ["ja", "en"])
@pytest.mark.parametrize(
    "scale,size",
    [(1.3333, "1080x820"), (2.0, "800x600"), (2.6667, "640x480"), (2.6667, "360x900")],
)
def test_editor_has_one_viewport_and_persistent_document_navigation(
    monkeypatch, language, scale, size
):
    root, app, errors, _, callbacks = make_app(
        monkeypatch, scale, language, initial_task=None
    )
    try:
        root.geometry(size)
        settle(root)
        visible(app.editor_empty_new_button, root)
        visible(app.editor_empty_open_button, root)
        app.editor_empty_new_button.invoke()
        d = app.editor_new_dialog
        for name, value in {"ot_name": "Test", "tid": "1", "sid": "2"}.items():
            d.vars[name].set(value)
        d.create_button.invoke()
        settle(root)
        widget = app.editor_field_widgets["nickname"]
        areas = [area for area in app.scroll_areas if area.contains(widget)]
        assert len(areas) == 1 and not app.body.contains(widget)
        assert (
            app.editor_workspace.winfo_viewable()
            and not app.body.frame.winfo_ismapped()
        )
        areas[0].canvas.yview_moveto(1)
        settle(root)
        if app._compact_editor:
            for w in (
                app.editor_page_combo,
                app.editor_file_button,
                app.editor_info_button,
            ):
                visible(w, root)
            for index in (1, 2, 3, 0):
                app.editor_page_combo.current(index)
                app.editor_page_combo.event_generate("<<ComboboxSelected>>")
                settle(root)
                assert app.editor_notebook.index(app.editor_notebook.select()) == index
            app.editor_file_menu.post(root.winfo_rootx() + 12, root.winfo_rooty() + 80)
            root.update_idletasks()
            app.editor_file_menu.tk.call(str(app.editor_file_menu), "postcascade", 3)
            root.update_idletasks()
            assert app.editor_menu.winfo_ismapped()
            app.editor_file_menu.unpost()
            app.editor_menu.unpost()
        else:
            for w in (
                app.editor_new_button,
                app.editor_open_button,
                app.editor_more_button,
            ):
                visible(w, root)
        visible(app.editor_save_button, root)
        visible(app.stop_button, root)
        visible(app.exit_button, root)
        assert not errors and not callbacks
    finally:
        cleanup(root)


@pytest.mark.parametrize("kind", ["species", "moves", "abilities", "items"])
def test_catalog_paging_reaches_every_result_without_modifying_data(monkeypatch, kind):
    from zasim.catalog_picker import CatalogPicker

    root, app, errors, _, callbacks = make_app(monkeypatch, initial_task=None)
    try:
        accepted = []
        d = CatalogPicker(
            root,
            tk=app.tk,
            ttk=app.ttk,
            kind=kind,
            title="Test",
            accept=lambda c: accepted.append(c) or True,
        )
        seen = []
        while True:
            assert len(d.rows) <= d.LIMIT
            seen.extend(d.rows)
            if "disabled" in d.next_button.state():
                break
            d.next_button.invoke()
            settle(root)
        assert tuple(seen) == ui_catalog.choices(kind) and not accepted
        last = d.page
        d.next_button.invoke()
        assert d.page == last
        if last:
            d.previous_button.invoke()
            assert d.page == last - 1
        d.query.set("not-a-result-0000")
        assert not d.rows and d.page == 0
        assert all(
            "disabled" in w.state()
            for w in (d.previous_button, d.next_button, d.choose)
        )
        d.cancel.invoke()
        assert not d.query.trace_info() and not accepted
        assert not errors and not callbacks
    finally:
        cleanup(root)


def test_catalog_reopens_at_current_selection_and_accepting_it_is_not_an_edit(
    monkeypatch,
):
    root, app, errors, _, callbacks = make_app(monkeypatch, initial_task=None)
    try:
        doc = document(app)
        choice = ui_catalog.choices("items")[-1]
        doc.apply({"held_item": choice.value})
        app._editor_populate()
        settle(root)
        values = {n: v.get() for n, v in app.editor_vars.items()}
        raw = doc.record.data
        app._editor_pick_catalog("held_item")
        d = app.catalog_dialog
        assert d.page > 0 and d.rows[int(d.tree.selection()[0])].value == choice.value
        d.cancel.invoke()
        assert (
            values == {n: v.get() for n, v in app.editor_vars.items()}
            and doc.record.data == raw
        )
        app._editor_pick_catalog("held_item")
        d = app.catalog_dialog
        d.choose.invoke()
        assert (
            not d.window.winfo_exists()
            and not app.editor_form_dirty
            and doc.record.data == raw
        )
        assert not errors and not callbacks
    finally:
        cleanup(root)


def test_picker_blank_double_click_does_not_accept_stale_selection(monkeypatch):
    from types import SimpleNamespace

    root, app, errors, _, callbacks = make_app(monkeypatch, initial_task=None)
    try:
        doc = document(app)
        app._editor_pick_catalog("move1")
        d = app.catalog_dialog
        d.query.set("85")
        settle(root)
        d._first()
        settle(root)
        before = {n: v.get() for n, v in app.editor_vars.items()}
        y = d.tree.winfo_height() - 2
        assert not d.tree.identify_row(y)
        d._double_click(SimpleNamespace(y=y))
        assert d.window.winfo_exists() and before == {
            n: v.get() for n, v in app.editor_vars.items()
        }
        d.cancel.invoke()
        assert not errors and not callbacks
    finally:
        cleanup(root)


@pytest.mark.parametrize("language", ["ja", "en"])
def test_new_input_correction_clears_only_owned_error_state_and_traces(
    monkeypatch, language
):
    root, app, errors, _, callbacks = make_app(
        monkeypatch, language=language, initial_task=None
    )
    try:
        app.editor_new_button.invoke()
        d = app.editor_new_dialog
        d.create_button.invoke()
        assert errors and "invalid" in d.widgets["ot_name"].state()
        d.vars["ot_name"].set("Test")
        assert "invalid" not in d.widgets["ot_name"].state()
        d._focus_error("tid")
        d.generate_ids_button.invoke()
        assert "invalid" not in d.widgets["tid"].state()
        d.search_var.set("ちこりーた")
        assert any(v.startswith("152:0 ") for v in d.widgets["species"]["values"])
        v = d.vars["tid"]
        token = v.trace_add("write", lambda *_: None)
        d.cancel_button.invoke()
        assert v.trace_info() == [(("write",), token)]
        assert all(
            not value.trace_info() for name, value in d.vars.items() if name != "tid"
        )
        v.trace_remove("write", token)
        assert not callbacks
    finally:
        cleanup(root)


@pytest.mark.parametrize("language", ["ja", "en"])
def test_keyboard_section_switch_stays_in_editor_and_tracks_compact_selector(
    monkeypatch, language
):
    root, app, errors, _, callbacks = make_app(
        monkeypatch, 2.6667, language, initial_task=None
    )
    try:
        root.geometry("640x480")
        doc = document(app)
        raw = doc.record.data
        entry = app.editor_field_widgets["nickname"]
        entry.focus_force()
        settle(root)
        entry.event_generate("<Control-Tab>")
        settle(root)
        assert app.notebook.select() == str(app.editor_tab)
        assert app.editor_notebook.index(app.editor_notebook.select()) == 1
        assert app.editor_page_combo.current() == 1
        assert doc.record.data == raw and not errors and not callbacks
    finally:
        cleanup(root)


@pytest.mark.parametrize("language", ["ja", "en"])
def test_editor_scroll_extent_tracks_shrinking_form_without_blank_tail(
    monkeypatch, language
):
    root, app, errors, _, callbacks = make_app(
        monkeypatch, 1.3333, language, initial_task=None
    )
    try:
        root.geometry("640x480")
        document(app)
        app.editor_advanced_var.set(True)
        app._editor_toggle_details()
        settle(root)
        app.editor_advanced_var.set(False)
        app._editor_toggle_details()
        root.geometry("1080x820")
        settle(root)
        area = next(
            a
            for a in app.scroll_areas
            if a.contains(app.editor_field_widgets["nickname"])
        )
        region = area.canvas.bbox(area.window)
        assert region[3] == max(
            area.canvas.winfo_height(), area.content.winfo_reqheight()
        )
        assert region[2] == max(
            area.canvas.winfo_width(), area.content.winfo_reqwidth()
        )
        assert not errors and not callbacks
    finally:
        cleanup(root)


@pytest.mark.parametrize("language", ["ja", "en"])
@pytest.mark.parametrize("width", [360, 640])
def test_picker_heading_and_paging_controls_fit_narrow_high_dpi(
    monkeypatch, language, width
):
    root, app, errors, _, callbacks = make_app(
        monkeypatch, 2.6667, language, initial_task=None
    )
    try:
        root.geometry(f"{width}x900")
        document(app)
        app._editor_pick_catalog("held_item")
        d = app.catalog_dialog
        settle(root)
        for w in (
            d.search_label,
            d.entry,
            d.clear,
            d.previous_button,
            d.next_button,
            d.choose,
            d.cancel,
        ):
            visible(w, d.window)
            assert w.winfo_height() >= w.winfo_reqheight()
        assert d.search_label.winfo_width() >= d.search_label.winfo_reqwidth()
        d.cancel.invoke()
        assert not errors and not callbacks
    finally:
        cleanup(root)
