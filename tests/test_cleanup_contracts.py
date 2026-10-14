"""Frozen cleanup controls; synthetic boundaries are NOT a live bug report."""
from types import SimpleNamespace
import pytest
import zasim.client as client
from zasim.reliable import parse_reliable
from zasim.trade_protocol import FinalAgreement
from test_za_client import (_occupied_party, _install_live_boundary_fakes,
                            _synthetic_trade_outbound, _reliable_message)


def test_real_engine_completion_queues_final_and_requires_separate_ack():
    engines = [client.ReliableTradeEngine(offer=_occupied_party(n).to_bytes(encrypted=True),
                                        auto_confirm=True) for n in (25, 133)]
    finals = [0, 0]
    completed_with_pending = [False, False]
    for tick in range(200):
        now = tick / 50
        for index, engine in enumerate(engines):
            output = engine.drain(now=now)
            if engine.complete and engine.unacknowledged:
                completed_with_pending[index] = True
            for item in output:
                frame = parse_reliable(item.payload)
                if frame.is_application_data and frame.payload[:2] == b'\x01\x04':
                    finals[index] += 1
                    assert engine.unacknowledged
                engines[1-index].feed_message(_reliable_message(frame), now=now)
        if all(e.complete and not e.unacknowledged for e in engines):
            break
    assert all(e.complete and not e.unacknowledged for e in engines)
    assert finals == [1, 1]
    assert any(completed_with_pending)


@pytest.mark.parametrize('outbound,pending,completed', [
    (True, False, True), (True, True, False), (False, False, None),
])
def test_injected_completion_contract_is_distinct_from_real_engine(monkeypatch, tmp_path, outbound, pending, completed):
    events = []
    _install_live_boundary_fakes(
        monkeypatch, events=events,
        outbound_factory=lambda: (_synthetic_trade_outbound(FinalAgreement(2)),) if outbound else (),
        send_callback=lambda: None, trade_complete=True,
        trade_unacknowledged=(0xfff0,) if pending else (),
    )
    clock = [0.0]
    monkeypatch.setattr(client.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(client.time, 'sleep', lambda _: clock.__setitem__(0, clock[0] + .01))
    def run():
        return client.run_live_trade(_occupied_party(), auto_confirm=True,
            transport_factory=client.LiveTransport, output=tmp_path/'synthetic.epa9',
            settle_grace=0, timeout=.2, log=lambda _: None)
    if not outbound:
        # Retain the predecessor's synthetic-state counterexample, but require
        # fail-closed rejection instead of a receipt with no actual send.
        with pytest.raises(client.ClientProtocolError, match='before local FinalAgreement send'):
            run()
        assert not list(tmp_path.iterdir()) and events == ['start', 'stop']
        return
    result = run()
    assert result.completed is completed
    assert result.native_save_sync_complete is False
    assert result.output_path.read_bytes() == result.received_party_pa9
    assert sum(isinstance(e, tuple) and e[0] == 'send' for e in events) == int(outbound)
    # Even this send-boundary fixture is synthetic, not RF or game-save evidence.
    assert events[0] == 'start' and events[-1] == 'stop'


@pytest.mark.parametrize('attributes,expected', [
    ({'delta':120},-1), ({'delta':-240},2), ({'delta':1},-1),
    ({'num':4},-1), ({'num':5},1), ({'delta':0},0),
    ({'delta':'invalid'},0), ({'delta':None},0), ({},0),
])
def test_current_wheel_input_contract(attributes, expected):
    from zasim.gui import _DesktopApp
    assert _DesktopApp._wheel_units(SimpleNamespace(**attributes)) == expected


# Same mapped native binding controls pass on the unchanged predecessor.
from test_gui_layout import make_app, settle, cleanup


@pytest.mark.parametrize('language', ['en', 'ja'])
@pytest.mark.parametrize('event,delta,direction', [
    ('<MouseWheel>', 120, -1), ('<MouseWheel>', -120, 1),
    ('<Button-4>', None, -1), ('<Button-5>', None, 1),
])
def test_actual_root_wheel_binding_scrolls_editor_only(monkeypatch, language, event, delta, direction):
    root, app, errors, infos, callbacks = make_app(monkeypatch, language=language)
    try:
        if event.startswith('<Button') and root.tk.call('tk', 'windowingsystem') != 'x11':
            pytest.skip('X11 button events; MouseWheel cases cover the other route')
        root.geometry('800x600')
        settle(root)
        app.notebook.select(app.editor_tab)
        area = app.scroll_areas[3]
        area.frame.master.select(area.frame)
        settle(root)
        assert root.bind(event)
        area.canvas.configure(scrollregion=(0, 0, 2000, 5000))
        app.body.canvas.yview_moveto(.5)
        area.canvas.yview_moveto(.5)
        before, outer = area.canvas.yview()[0], app.body.canvas.yview()
        area.content.event_generate(event, **({} if delta is None else {'delta': delta}))
        assert (area.canvas.yview()[0] - before) * direction > 0
        assert app.body.canvas.yview() == outer
        assert not errors and not callbacks
    finally:
        cleanup(root)
