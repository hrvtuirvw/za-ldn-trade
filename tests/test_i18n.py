"""Localization is presentation only: never change pending edits or permissions."""
import ast
import json
from pathlib import Path
import re

import pytest
from zasim import i18n


@pytest.fixture(autouse=True)
def restore_language():
    before = i18n.get_language()
    yield
    i18n.set_language(before)


def test_catalog_complete_with_matching_placeholders():
    catalog = i18n.english_catalog()
    assert len(catalog) >= 290
    for source, target in catalog.items():
        assert i18n._fields(source) == i18n._fields(target)
        assert not re.search('[\u3040-\u30ff\u3400-\u9fff]', target), source
    root = Path(__file__).resolve().parents[1] / 'zasim'
    for name in ('gui.py', 'pa9_create_dialog.py', 'doctor.py', 'wireless.py', 'radio_picker.py'):
        tree = ast.parse((root / name).read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'tr':
                if node.args and isinstance(node.args[0], ast.Constant):
                    assert node.args[0].value in catalog, (name, node.lineno, node.args[0].value)


@pytest.mark.parametrize('language,text', [('ja','待機中'),('en','Ready')])
def test_message_formatting_does_not_interpret_user_values(language,text):
    i18n.set_language(language)
    assert i18n.tr('待機中') == text
    assert '{not_a_field}' in i18n.tr('PA9を保存しました: {v0}', v0='{not_a_field}')
    with pytest.raises(ValueError): i18n.set_language('unsupported')


def test_preference_atomic_merge_and_invalid_file_preservation(tmp_path):
    target = tmp_path/'preferences.json'
    target.write_text('{"other_setting": 42}')
    i18n.save_language('en', path=target)
    assert json.loads(target.read_text()) == {'other_setting':42,'language':'en'}
    assert not list(tmp_path.glob('*.tmp'))
    target.write_text('invalid json')
    with pytest.raises(ValueError): i18n.save_language('ja', path=target)
    assert target.read_text() == 'invalid json'


@pytest.mark.parametrize('locale,expected', [('ja_JP.UTF-8','ja'),('en_US.UTF-8','en'),('fr_FR.UTF-8','en')])
def test_locale_selection(monkeypatch, locale, expected):
    monkeypatch.delenv('ZA_TRADE_LANG',raising=False)
    monkeypatch.setenv('LC_ALL',locale)
    assert i18n.detect_language() == expected
    monkeypatch.setenv('ZA_TRADE_LANG','ja')
    assert i18n.detect_language() == 'ja'


def test_preference_priority_and_read_only_startup(monkeypatch,tmp_path):
    path=tmp_path/'preferences.json'
    monkeypatch.setattr(i18n,'preference_path',lambda:path)
    monkeypatch.delenv('ZA_TRADE_LANG',raising=False)
    monkeypatch.setenv('LC_ALL','en_US.UTF-8')
    assert i18n.preferred_language()=='en' and not path.exists()
    i18n.save_language('ja',path=path)
    assert i18n.preferred_language()=='ja'
    monkeypatch.setenv('ZA_TRADE_LANG','en')
    assert i18n.preferred_language()=='en'


@pytest.mark.parametrize('size,scale', [('1080x820',1.3333),('800x600',2.0),('640x480',2.6667)])
def test_bilingual_switch_preserves_unapplied_document_and_controls(monkeypatch,tmp_path,size,scale):
    from test_gui_layout import make_app,settle,cleanup,reveal,visible,descendants
    from zasim.pa9_creation import CreateRequest
    from zasim.pa9_editor import PA9EditorDocument
    root,app,errors,infos,callbacks=make_app(monkeypatch,scale)
    try:
        root.geometry(size)
        document=PA9EditorDocument.create(CreateRequest(152,'TEST',1,2,language=2,seed=11))
        app.editor_document=document
        app._editor_populate()
        app.editor_vars['nickname'].set('UNSAVED')
        app.editor_vars['nature'].set('3: いじっぱり')
        app.editor_vars['stat_nature'].set('0: がんばりや')
        app.editor_vars['ev_hp'].set('252')
        app.nickname_var.set('MY-PC')
        app.link_code_var.set('12345678')
        app.output_var.set(str(tmp_path/'out.epa9'))
        original=document.record.data
        app._append_log('retained log')
        app._show_result({'retained':'result'})
        app.notebook.select(app.editor_tab)
        for language in ('en','ja','en'):
            assert app.change_language(language)
            settle(root)
            assert app.editor_document is document and document.record.data==original
            assert app.editor_vars['nickname'].get()=='UNSAVED'
            assert app.editor_vars['ev_hp'].get()=='252'
            assert app.editor_vars['nature'].get().startswith('3: ')
            assert app.editor_form_dirty
            assert app.nickname_var.get()=='MY-PC' and app.link_code_var.get()=='12345678'
            assert app.output_var.get()==str(tmp_path/'out.epa9')
            assert 'retained log' in app.log_text.get('1.0','end')
            assert 'retained' in app.result_text.get('1.0','end')
            assert app.notebook.select()==str(app.editor_tab)
            assert not app.connection_panel.winfo_ismapped()
            assert app.auto_confirm_var.get() is False
            app.editor_advanced_var.set(True);app._editor_toggle_details();settle(root)
            if app._compact_editor:
                for button in (app.editor_file_button, app.editor_page_combo, app.editor_info_button):
                    visible(button, root)
                assert app.editor_file_menu.entrycget(0, 'label') == i18n.tr('新規作成')
                assert app.editor_menu.entrycget(0, 'label') == i18n.tr('適用')
            else:
                for button in (app.editor_new_button, app.editor_apply_button):
                    visible(button, root)
            visible(app.editor_save_as_button, root)
            visible(app.stop_button,root);visible(app.exit_button,root)
            if language=='en':
                assert 'Hardy' in app.editor_vars['stat_nature'].get()
                for widget in (*descendants(app.body.content), *descendants(app.editor_workspace)):
                    if widget.winfo_class() in ('TLabel','TButton','TCheckbutton','TLabelframe'):
                        text=str(widget.cget('text'))
                        assert not re.search('[\u3040-\u30ff\u3400-\u9fff]',text) or text=='日本語 / English',text
        app.notebook.select(app.trade_tab);settle(root)
        assert app.connection_panel.winfo_ismapped()
        assert app._connection().single_interface is None
        assert not errors and not callbacks
    finally:cleanup(root)


def test_language_change_during_job_does_not_rebuild_or_cancel(monkeypatch):
    from test_gui_layout import make_app,cleanup
    root,app,*_=make_app(monkeypatch)
    try:
        before=app.outer
        monkeypatch.setattr(type(app.controller),'is_busy',property(lambda self:True))
        assert not app.change_language('en')
        assert app.outer is before and app.language=='ja'
        assert app.language_var.get()=='日本語'
    finally:cleanup(root)


def test_english_new_dialog_and_validation(monkeypatch):
    from test_gui_layout import make_app,settle,cleanup
    root,app,errors,_,callbacks=make_app(monkeypatch)
    try:
        app.change_language('en');app._editor_new();settle(root)
        dialog=app.editor_new_dialog
        assert dialog.vars['language'].get()=='English'
        assert dialog.create_button.cget('text')=='Create & edit'
        assert all(not re.search('[\u3040-\u30ff\u3400-\u9fff]',label) for label in dialog.species_labels)
        dialog.submit()
        assert errors[-1][0]=='Could not create PA9'
        dialog.cancel_button.invoke()
        assert not callbacks
    finally:cleanup(root)


def test_catalog_has_no_orphan_source_ids():
    # Dynamic field labels are literal declarations too; do not prune only by
    # scanning direct tr(...) calls or by searching escaped source text.
    package = Path(i18n.__file__).parent
    strings = set()
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        strings.update(node.value for node in ast.walk(tree)
                       if isinstance(node, ast.Constant) and isinstance(node.value, str))
    assert not (set(i18n.english_catalog()) - strings)


@pytest.mark.parametrize("name", ["TESTING.md", "WIRELESS_COMPATIBILITY.md",
                                  "WIRELESS_COMPATIBILITY_EN.md", "PA9_CREATION.md"])
def test_current_guides_do_not_label_historical_releases_as_current(name):
    text = (Path(__file__).resolve().parents[1] / "docs" / name).read_text(encoding="utf-8")
    # Historical evidence keeps its version; evergreen guide headings and the
    # current-path row must not silently retain an older release label.
    assert not re.search(r"Current [0-9]+\.[0-9]+\.[0-9]+", text)
    assert not re.search(r"(?m)^.*(?:Updated:|更新:).* / ZA Trade Lab [0-9]", text)
    assert not re.search(r"(?m)^# .*（[0-9]+\.[0-9]+\.[0-9]+）", text)
