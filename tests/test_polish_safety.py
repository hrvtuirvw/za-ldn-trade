"""Positive/fault controls: optimization must not weaken display or wire guards."""
from dataclasses import replace
import pytest
from test_runtime_polish import editor
from test_gui_layout import cleanup


@pytest.mark.parametrize('mutation', ['checksum', 'sanity', 'empty', 'stored', 'flags', 'subclass'])
def test_status_matches_fresh_validation_after_every_document_change(monkeypatch, mutation):
    import zasim.gui as gui
    from zasim.pa9 import PA9
    root, app, errors, callbacks = editor(monkeypatch)
    try:
        doc = app.editor_document
        if mutation == 'checksum':doc.record.nickname = 'Changed'
        elif mutation == 'sanity':doc.record.sanity = 1
        elif mutation == 'empty':doc.record.species_internal = 0
        elif mutation == 'stored':doc.record = PA9(doc.record.to_bytes(party=False), encrypted=False)
        elif mutation == 'flags':
            doc.input_encrypted = True; doc.auto_recalculate = False
            doc.derived_stats_may_be_stale = True
        else:
            class MutableHeader(PA9):
                sanity_override = 0
                @property
                def sanity(self):return self.sanity_override
            doc.record = MutableHeader(doc.record.data, encrypted=False, validate=False)
            app._editor_update_status()
            # Same bytes/type, different subclass property contract. Cache only
            # the concrete byte-backed PA9 implementation, not arbitrary types.
            doc.record.sanity_override = 1
        app._editor_update_status()
        issues = gui.validate_editor_record(doc.record)
        expected_errors = sum(i.severity=='error' for i in issues)
        expected_warnings = sum(i.severity=='warning' for i in issues)
        status=app.editor_status_var.get()
        assert f'error {expected_errors}' in status
        assert f'warning {expected_warnings}' in status
        assert ('INVALID' in status) is (not doc.record.checksum_valid)
        if mutation=='stored':assert 'Stored' in status
        if mutation=='flags':
            assert '暗号化' in status and '手動編集' in app.editor_notice_var.get()
            assert '警告' in app.editor_notice_var.get()
        assert not errors and not callbacks
    finally:cleanup(root)


@pytest.mark.parametrize('failed_index', [0, 1, 5])
def test_partial_bulk_failure_restores_callbacks_and_preserves_record(monkeypatch, failed_index):
    root, app, _, _ = editor(monkeypatch)
    try:
        before=app.editor_document.record.data
        variables=[v for n,v in app.editor_vars.items() if n.startswith('iv_')]
        def failed(_):raise RuntimeError('synthetic variable-set failure')
        monkeypatch.setattr(variables[failed_index], 'set', failed)
        with pytest.raises(RuntimeError, match='synthetic variable'):
            app._editor_set_fields('iv_', '1')
        assert not app._editor_populating
        assert app.editor_document.record.data==before
        if failed_index:
            assert app.editor_form_dirty and all(v.get()=='1' for v in variables[:failed_index])
        app.editor_vars['nickname'].set('After failure')
        assert app.editor_form_dirty and app._editor_is_dirty()
    finally:cleanup(root)


def test_outer_population_mode_is_preserved_by_bulk_edit(monkeypatch):
    root,app,_,_=editor(monkeypatch)
    try:
        app._editor_populating=True; app.editor_form_dirty=False
        app._editor_set_fields('iv_', '1')
        assert app._editor_populating and not app.editor_form_dirty
    finally:cleanup(root)


def test_unloaded_document_drops_presentation_cache(monkeypatch):
    root,app,_,_=editor(monkeypatch)
    try:
        app.editor_document=None;app._editor_update_status()
        assert getattr(app,'_editor_status_cache',None) is None
        assert app.editor_status_var.get()=='未読込'
    finally:cleanup(root)


@pytest.mark.parametrize('protocol', [0, 2, 10, 11, 20, 255])
def test_standard_handshake_still_refuses_non_generic_outbox(monkeypatch, protocol):
    import zasim.handshake as handshake
    import test_handshake_public as fixture
    from zasim.session import OutboundPiaMessage
    original_session=fixture.ZASession
    class PoisonedSession(original_session):
        def drain(self):
            return super().drain()+(OutboundPiaMessage(protocol,b'forbidden',1,1),)
    transports=[]
    original_start=handshake.start_transport
    def started(transport, cancel_event):
        transports.append(transport);return original_start(transport,cancel_event)
    monkeypatch.setattr(fixture,'ZASession',PoisonedSession)
    monkeypatch.setattr(handshake,'start_transport',started)
    with pytest.raises(handshake.HandshakeSafetyError, match='non-generic'):
        fixture.test_real_generic_handshake_never_sends_trade_and_preserves_errors(monkeypatch,True,None)
    assert len(transports)==1 and transports[0].starts==transports[0].stops==1


@pytest.mark.parametrize('language', ['ja', 'en'])
@pytest.mark.parametrize('case', ['extra_sanity', 'extra_checksum', 'hidden_sanity', 'hidden_checksum'])
def test_subclass_status_never_seeds_concrete_record_cache(monkeypatch, language, case):
    import zasim.gui as gui
    from zasim.pa9 import PA9
    from zasim.i18n import get_language, set_language
    previous_language = get_language()
    root, app, errors, callbacks = editor(monkeypatch)
    try:
        set_language(language)
        plain = PA9(app.editor_document.record.data, encrypted=False)
        if case == 'hidden_sanity':
            plain.sanity = 1
        if case == 'hidden_checksum':
            plain.nickname = 'Checksum'
            assert not plain.checksum_valid
        raw = plain.data
        class Derived(PA9):
            @property
            def sanity(self):
                if case == 'extra_sanity':return 1
                if case == 'hidden_sanity':return 0
                return super().sanity
            @property
            def checksum_valid(self):
                if case == 'extra_checksum':return False
                if case == 'hidden_checksum':return True
                return super().checksum_valid
        app.editor_document.record = Derived(raw, encrypted=False, validate=False)
        app._editor_update_status()
        subclass_status = app.editor_status_var.get()
        # Loading another document/populating the form must not reuse a prior
        # subclass's validation even when the underlying byte snapshot matches.
        app.editor_document = replace(app.editor_document, record=plain)
        calls = []
        original = gui.validate_editor_record
        def counted(record):
            calls.append(type(record))
            return original(record)
        monkeypatch.setattr(gui, 'validate_editor_record', counted)
        app._editor_populate()
        issues = original(plain)
        status = app.editor_status_var.get()
        error_count = sum(i.severity == "error" for i in issues)
        assert (f'Errors: {error_count}' if language == 'en' else f'error {error_count}') in status
        warning_count = sum(i.severity == "warning" for i in issues)
        assert (f'warnings: {warning_count}' if language == 'en' else f'warning {warning_count}') in status
        assert ('INVALID' in status) is (not plain.checksum_valid)
        assert status != subclass_status
        assert calls == [PA9]
        app.editor_vars['nickname'].set('Pending')
        app._editor_update_status()
        assert calls == [PA9], 'unchanged concrete record should remain cached'
        assert app.editor_document.record.data == raw
        assert app.editor_form_dirty
        assert not errors and not callbacks
    finally:
        cleanup(root)
        set_language(previous_language)
