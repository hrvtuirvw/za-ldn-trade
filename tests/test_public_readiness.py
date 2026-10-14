"""Public-input/runtime boundaries. Synthetic peers only; never host radio IO."""
from __future__ import annotations

import json
import socket
import threading
from unittest.mock import Mock

import pytest
import zstandard

from zasim import capture, live, pia, relay
from zasim.control import OperationCancelled
from test_review_hardening import desktop, _party


@pytest.mark.parametrize('known_size', [False, True])
@pytest.mark.parametrize('suffix', ['garbage', 'frame', 'skippable'])
@pytest.mark.parametrize('authenticated', [False, True])
def test_compressed_payload_rejects_every_unconsumed_byte(known_size, suffix, authenticated):
    blob = zstandard.ZstdCompressor(write_content_size=known_size).compress(b'valid compact bytes')
    tails = {'garbage': b'\x01invalid', 'frame': blob,
             'skippable': bytes.fromhex('502a4d18') + bytes(4)}
    raw = blob + tails[suffix]
    with pytest.raises(pia.CompressionError):
        if authenticated:
            crypto = pia.PiaCrypto(bytes(range(16)))
            header = pia.PiaHeader(nonce=b'oneframe', flags=pia.PACKET_FLAG_ZSTD)
            packet = crypto.encrypt(raw, '169.254.1.1', header)
            pia.decode_datagram(packet, '169.254.1.1', crypto=crypto, parse=False)
        else:
            pia.decompress_zstd(raw, max_output_size=128)


@pytest.mark.parametrize('known_size', [False, True])
@pytest.mark.parametrize('size', [0, 1, 128])
def test_exact_compressed_frame_stays_accepted(known_size, size):
    raw = bytes(range(128))[:size]
    blob = zstandard.ZstdCompressor(write_content_size=known_size).compress(raw)
    assert pia.decompress_zstd(blob, max_output_size=128) == raw
    with pytest.raises(pia.CompressionError):
        pia.decompress_zstd(blob[:-1], max_output_size=128)


@pytest.mark.parametrize('phase', ['setsockopt', 'settimeout'])
def test_relay_accepted_socket_is_closed_if_initial_setup_fails(phase):
    connection = Mock()
    error = OSError('synthetic accepted-socket setup failure')
    getattr(connection, phase).side_effect = error
    factory = Mock(side_effect=AssertionError('transport must not be constructed'))
    server = relay.ZARelayServer(keys_path=None, transport_factory=factory, log=lambda _: None)
    server.listener = Mock()
    server.listener.accept.return_value = (connection, ('127.0.0.1', 12345))
    with pytest.raises(OSError) as caught:
        server.serve_once()
    assert caught.value is error
    assert connection.close.call_count == 1
    factory.assert_not_called()
    assert server._failed_transport is None


def test_relay_listener_is_closed_if_bound_address_query_fails(monkeypatch):
    listener = Mock()
    error = OSError('synthetic getsockname failure')
    listener.getsockname.side_effect = error
    monkeypatch.setattr(relay.socket, 'socket', lambda *a: listener)
    server = relay.ZARelayServer(keys_path=None, transport_factory=Mock(), log=lambda _: None)
    with pytest.raises(OSError) as caught:
        server.open()
    assert caught.value is error
    listener.close.assert_called_once()
    assert server.listener is None and server.bound_endpoint is None


def test_relay_initial_failure_closes_real_accepted_descriptor(monkeypatch):
    accepted, peer = socket.socketpair()
    server = relay.ZARelayServer(keys_path=None, transport_factory=Mock(), log=lambda _: None)
    server.listener = Mock()
    server.listener.accept.return_value = (accepted, ('127.0.0.1', 12345))
    monkeypatch.setattr(socket.socket, 'setsockopt', Mock(side_effect=OSError('setup fault')))
    try:
        with pytest.raises(OSError):
            server.serve_once()
        assert accepted.fileno() == -1
        assert peer.recv(1) == b''
    finally:
        accepted.close(); peer.close()


@pytest.mark.parametrize('known_size', [False, True])
def test_compressed_frames_every_truncation_and_boundary(known_size):
    import random
    rng = random.Random(0x20261010)
    for size in (0, 1, 31, 128, 512, 1400):
        value = rng.randbytes(size)
        frame = zstandard.ZstdCompressor(write_content_size=known_size).compress(value)
        assert pia.decompress_zstd(frame, max_output_size=1400) == value
        for end in range(len(frame)):
            with pytest.raises(pia.CompressionError):
                pia.decompress_zstd(frame[:end], max_output_size=1400)
    bomb = zstandard.ZstdCompressor(write_content_size=known_size).compress(bytes(65536))
    with pytest.raises(pia.CompressionError):
        pia.decompress_zstd(bomb, max_output_size=128)


class ProbeTransport:
    ssid = bytes(range(16))
    our_ip = '169.254.1.2'
    host_ip = '169.254.1.1'
    LOCAL_COMMUNICATION_ID = 1
    SCENE_ID = 1
    network_app_version = 1
    app_data = b''
    def __init__(self):
        self.starts = self.stops = self.receives = 0
    def start(self, cancel_event=None):
        self.starts += 1
        return self
    def recv(self):
        self.receives += 1
        return [(b'synthetic invalid packet', self.host_ip)] * 8
    def stop(self):
        self.stops += 1


@pytest.mark.parametrize('boundary', ['cancel', 'deadline'])
def test_passive_capture_checks_limits_inside_each_received_batch(tmp_path, monkeypatch, boundary):
    transport = ProbeTransport()
    cancelled = threading.Event()
    clock = [0.0]
    monkeypatch.setattr(live.time, 'monotonic', lambda: clock[0])
    original = capture.CaptureWriter.packet
    writes = []
    def packet(self, *args, **kwargs):
        original(self, *args, **kwargs)
        writes.append(True)
        clock[0] = 2.0
        if boundary == 'cancel':
            cancelled.set()
    monkeypatch.setattr(capture.CaptureWriter, 'packet', packet)
    path = tmp_path / 'capture.jsonl'
    def run():
        return live.run_passive_probe(output=path, duration=1, transport_factory=lambda: transport,
                                      cancel_event=cancelled, log=lambda _: None)
    if boundary == 'cancel':
        with pytest.raises(OperationCancelled):
            run()
    else:
        result = run()
        assert result['packets'] == 1
    assert writes == [True]
    assert transport.starts == transport.stops == transport.receives == 1
    records = [json.loads(row) for row in path.read_text().splitlines()]
    assert [r['rec'] for r in records] == ['meta', 'pkt']
    with path.open('a') as stream:
        stream.write('')  # Writer has closed/flushed before completion.


@pytest.mark.parametrize('operation', ['trade', 'probe'])
def test_broken_diagnostic_log_cannot_disable_running_gui_stop(desktop, tmp_path, monkeypatch, operation):
    app, root, messages = desktop
    entered = threading.Event()
    def runner(*args, cancel_event, **kwargs):
        entered.set()
        assert cancel_event.wait(3)
        raise OperationCancelled('synthetic cancellation')
    app.controller.trade_runner = runner
    app.controller.probe_runner = runner
    app.link_code_var.set('12345678')
    offer = tmp_path / 'offer.epa9'; offer.write_bytes(_party(152))
    app.offer_var.set(str(offer))
    app.output_var.set(str(tmp_path / 'received.epa9'))
    app.capture_output_var.set(str(tmp_path / 'capture.jsonl'))
    app.auto_confirm_var.set(False)
    monkeypatch.setattr(app, '_append_log', Mock(side_effect=RuntimeError('synthetic log failure')))
    try:
        getattr(app, '_start_' + operation)()
        assert entered.wait(2)
        assert app.controller.is_busy
        assert 'disabled' not in app.stop_button.state()
        assert 'disabled' in app.trade_button.state()
        assert 'disabled' in app.probe_button.state()
        app.stop_button.invoke()
        assert app.controller.wait(3)
        app._poll()
        assert not app.controller.has_pending_result
    finally:
        app.controller.cancel()
        assert app.controller.wait(3)


@pytest.mark.parametrize('language,condition', [('en', 'If capture'), ('ja', '場合')])
def test_early_capture_cancellation_does_not_claim_a_file_was_saved(desktop, tmp_path, language, condition):
    from zasim.gui_controller import ControllerState, GuiEvent
    app, root, messages = desktop
    app.change_language(language)
    output = tmp_path / 'never-started.jsonl'
    app.capture_output_var.set(str(output))
    app.active_operation = 'probe'
    app.active_job_id = app.controller.job_id
    error = OperationCancelled('cancelled during connection startup')
    app.controller._finish(app.active_job_id, ControllerState.CANCELLED, 'cancelled', error)
    app._handle_terminal(GuiEvent(app.active_job_id, 'cancelled', error))
    assert not output.exists()
    assert condition in messages.showinfo.call_args.args[1]
