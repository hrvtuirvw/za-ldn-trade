"""Offline contracts for removal and redundant-work elimination."""
import inspect
import pytest

RESEARCH_OPTIONS = (
    'protocols', 'protocol_count_sweep', 'net_response_broadcast',
    'session_join_broadcast', 'session_join_compress', 'session_join_source_zero',
    'session_join_empty_mesh_extension', 'session_join_skip_network_check',
    'session_join_force_full_header', 'session_join_no_skip_destination',
    'session_join_wait_for_net_keepalive', 'session_join_fresh_endpoint_probe',
)

@pytest.mark.parametrize('option', RESEARCH_OPTIONS)
def test_public_handshake_does_not_carry_research_options(option):
    from zasim.handshake import run_handshake_diagnostic
    assert option not in inspect.signature(run_handshake_diagnostic).parameters


@pytest.mark.parametrize('pending', [False, True])
@pytest.mark.parametrize('grace', [0, .05])
def test_synthetic_completion_without_send_is_rejected_without_save(monkeypatch, tmp_path, pending, grace):
    import zasim.client as client
    from test_za_client import _occupied_party, _install_live_boundary_fakes
    events = []
    _install_live_boundary_fakes(monkeypatch, events=events, outbound_factory=lambda: (),
        send_callback=lambda: pytest.fail('no outbound exists'), trade_complete=True,
        trade_unacknowledged=(0xfff0,) if pending else ())
    clock = [0.0]
    monkeypatch.setattr(client.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(client.time, 'sleep', lambda _: clock.__setitem__(0, clock[0]+.01))
    target = tmp_path/'not-saved.epa9'
    with pytest.raises(client.ClientProtocolError, match='before local FinalAgreement send'):
        client.run_live_trade(_occupied_party(), auto_confirm=True,
            transport_factory=client.LiveTransport, output=target,
            timeout=.2, settle_grace=grace, log=lambda _: None)
    assert not list(tmp_path.iterdir())
    assert events == ['start', 'stop']


@pytest.mark.parametrize('streams', [1, 2, 4])
def test_capture_reassembler_is_allocated_only_for_new_streams(monkeypatch, streams):
    from test_capture import Decoded, Header, Message
    import zasim.capture as capture
    import zasim.reliable as reliable
    base = reliable.ReliableReassembler
    allocations = []
    class Counted(base):
        def __init__(self, *args, **kwargs):
            allocations.append(1); super().__init__(*args, **kwargs)
    monkeypatch.setattr(reliable, 'ReliableReassembler', Counted)
    state = {}; outputs = []
    for i in range(20):
        decoded = Decoded(Header(), False, b'', b'', [Message(10, 7,
            reliable.build_reliable(3, i//streams, b'opaque'))])
        outputs.append(capture.decoded_summary(decoded, reliable_reassemblers=state,
                                               reliable_stream=i % streams))
    assert len(allocations) == streams
    assert len(state) == streams
    assert all('recognized' not in out['messages'][0]['reliable'] for out in outputs)


def test_capture_explicit_none_stream_value_keeps_stateless_contract(monkeypatch):
    from test_capture import Decoded, Header, Message
    import zasim.capture as capture
    import zasim.reliable as reliable
    class Forbidden(reliable.ReliableReassembler):
        def __init__(self, *args, **kwargs):
            pytest.fail('existing None is not a missing stream')
    monkeypatch.setattr(reliable, 'ReliableReassembler', Forbidden)
    state = {'s':None}
    decoded = Decoded(Header(), False, b'', b'', [Message(10, 7,reliable.build_reliable(3, 0, b'opaque'))])
    result = capture.decoded_summary(decoded, reliable_reassemblers=state, reliable_stream='s')
    assert state == {'s':None}
    assert result['messages'][0]['reliable']['application_messages'][0]['payload_hex'] == b'opaque'.hex()


@pytest.mark.parametrize('missing', [False, True])
def test_personal_lookup_reads_record_identity_once(missing):
    from zasim.pa9_creation import _data, personal_for_record
    row = [p for p in _data()['personal'].values() if not p['battle_only']][-1]
    reads = []
    class Record:
        @property
        def species_internal(self):
            reads.append('species'); return 65535 if missing else row['internal_id']
        @property
        def form(self):
            reads.append('form'); return row['form']
    if missing:
        with pytest.raises(ValueError, match='no ZA personal data'):
            personal_for_record(Record())
    else:
        assert personal_for_record(Record()) == row
    assert reads == ['species', 'form']


def editor(monkeypatch):
    from test_gui_layout import make_app
    from zasim.pa9_editor import PA9EditorDocument
    from zasim.pa9_creation import CreateRequest
    root, app, errors, _, callbacks = make_app(monkeypatch)
    app.editor_document = PA9EditorDocument.create(CreateRequest(152, 'Test', 1, 2, seed=1))
    app._editor_populate()
    return root, app, errors, callbacks


def test_unapplied_fields_do_not_revalidate_unchanged_document(monkeypatch):
    import zasim.gui as gui
    from test_gui_layout import cleanup
    original = gui.validate_editor_record; calls = []
    def validated(record):
        calls.append(record.data); return original(record)
    monkeypatch.setattr(gui, 'validate_editor_record', validated)
    root, app, errors, callbacks = editor(monkeypatch)
    try:
        baseline = app.editor_document.record.data
        calls.clear()
        for value in ('One','Two','Three'):
            app.editor_vars['nickname'].set(value)
        assert calls == []
        assert app.editor_document.record.data == baseline and app.editor_form_dirty
        app.editor_document.record.sanity = 1
        app._editor_update_status()
        assert len(calls) == 1 and 'error 1' in app.editor_status_var.get()
        app._editor_update_status()
        assert len(calls) == 1
        assert not errors and not callbacks
    finally:cleanup(root)


def test_non_ev_input_does_not_rescan_ev_widgets(monkeypatch):
    from test_gui_layout import cleanup
    root, app, _, _ = editor(monkeypatch); calls = []
    original = app._editor_update_ev_total
    monkeypatch.setattr(app, '_editor_update_ev_total', lambda: (calls.append(1), original()))
    try:
        app.editor_vars['nickname'].set('One')
        app.editor_vars['iv_hp'].set('30')
        assert calls == []
        app.editor_vars['ev_hp'].set('100')
        assert calls == [1] and '100' in app.editor_ev_total_var.get()
    finally:cleanup(root)


@pytest.mark.parametrize('prefix', ['iv_', 'ev_'])
def test_bulk_field_update_refreshes_status_once(monkeypatch, prefix):
    from test_gui_layout import cleanup
    root, app, _, _ = editor(monkeypatch); calls = []
    before = app.editor_document.record.data
    original = app._editor_update_status
    monkeypatch.setattr(app, '_editor_update_status', lambda: (calls.append(1), original()))
    try:
        app._editor_set_fields(prefix, '1')
        assert calls == [1]
        assert all(v.get() == '1' for n,v in app.editor_vars.items() if n.startswith(prefix))
        assert app.editor_form_dirty and app.editor_document.record.data == before
    finally:cleanup(root)
