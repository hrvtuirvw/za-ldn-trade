from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import sys
import threading
import warnings

import pytest
import zasim.client as za_client

from zasim.client import (
    ACK_MESSAGE_FLAGS,
    BULK_ACK_FLAGS,
    ClientProtocolError,
    DATA_MESSAGE_FLAGS,
    INITIAL_RELIABLE_SEQUENCE,
    MAX_RECEIVED_COMMAND_HISTORY,
    ReliableTimeout,
    ReliableTradeEngine,
    ZA_RELIABLE_PROTOCOL_SPECIFIC,
    prepare_party_offer,
)
from zasim.pa9 import PA9
from zasim.pia import (
    MESSAGE_FIELD_FLAGS,
    MESSAGE_FIELD_PORT,
    MESSAGE_FIELD_PROTOCOL,
    MESSAGE_FIELD_PROTOCOL_SPECIFIC,
    MESSAGE_FIELD_SIZE,
    PiaMessage,
    build_message,
    parse_messages,
)
from zasim.reliable import (
    ReliableFlags,
    ReliableFrame,
    build_bulk_ack,
    parse_bulk_ack,
    parse_reliable,
)
from zasim.trade import TradeState
from zasim.trade_protocol import (
    ConfirmTrade,
    FinalAgreement,
    Ready,
    SelectPokemon,
    CompatibilityMatrix,
    encode_frame,
)


STREAM = ZA_RELIABLE_PROTOCOL_SPECIFIC
FIELDS = (
    MESSAGE_FIELD_FLAGS
    | MESSAGE_FIELD_SIZE
    | MESSAGE_FIELD_PROTOCOL
    | MESSAGE_FIELD_PORT
    | MESSAGE_FIELD_PROTOCOL_SPECIFIC
)


def _reliable_message(frame: ReliableFrame, stream: int = STREAM) -> PiaMessage:
    payload = frame.pack()
    message_flags = DATA_MESSAGE_FLAGS if frame.is_application_data else ACK_MESSAGE_FLAGS
    return PiaMessage(FIELDS, message_flags, len(payload), 10, 0, stream, payload)


def _application(command, sequence: int = INITIAL_RELIABLE_SEQUENCE) -> ReliableFrame:
    return ReliableFrame(
        ReliableFlags.APPLICATION_DATA
        | ReliableFlags.MESSAGE_START
        | ReliableFlags.MESSAGE_END
        | ReliableFlags.INITIALIZED,
        sequence,
        INITIAL_RELIABLE_SEQUENCE,
        encode_frame(command).wire_payload,
    )


def _ready() -> Ready:
    return Ready(0, CompatibilityMatrix(bytes(0x4B0)))


def _synthetic_trade_outbound(command):
    return SimpleNamespace(
        protocol=10,
        protocol_specific=STREAM,
        payload=_application(command).pack(),
    )


def _install_live_boundary_fakes(
    monkeypatch,
    *,
    events: list[object],
    outbound_factory,
    send_callback,
    stop_callback=lambda: None,
    trade_complete: bool = False,
    trade_unacknowledged: tuple[int, ...] = (),
    drain_after_emit_error: BaseException | None = None,
) -> None:
    remote_offer = _occupied_party(133).to_bytes(encrypted=True)

    class FakeTransport:
        def __init__(self):
            self.app_data = b"\x00\x5c\x16\x00\x01" + bytes(0x5C - 5)
            self.our_mac = b"\x10" * 6
            self.host_mac = b"\x20" * 6
            self.our_ip = "169.254.1.2"
            self.host_ip = "169.254.1.1"
            self.broadcast = "169.254.1.255"
            self.ssid = bytes(16)

        def start(self):
            events.append("start")

        def recv(self):
            return []

        def send(self, datagram, destination):
            events.append(("send", datagram, destination))
            send_callback()

        def stop(self):
            events.append("stop")
            stop_callback()

    class FakeTrade:
        def __init__(self, **_kwargs):
            self.remote_offer = remote_offer
            self.negotiation = SimpleNamespace(
                local_state=TradeState.OFFERED,
                remote_state=TradeState.OFFERED,
                remote_cancel_count=0,
            )
            # Do not let settle_grace=0 use the driver's dummy-completion
            # fallback before this fixture has actually emitted FinalAgreement.
            self.complete = False
            self.unacknowledged = trade_unacknowledged
            self.confirmed = False

        def confirm(self, *, now):
            self.confirmed = True
            self.negotiation.local_state = TradeState.FINAL_AGREEMENT

    class FakeClient:
        def __init__(self, _session, trade):
            self.trade = trade
            self.emitted = False

        def feed_decoded(self, *_args, **_kwargs):
            raise AssertionError("no incoming packet expected")

        def drain(self, *, now, tick):
            if self.trade.confirmed and not self.emitted:
                self.emitted = True
                self.trade.complete = trade_complete
                return tuple(outbound_factory())
            if self.emitted and drain_after_emit_error is not None:
                raise drain_after_emit_error
            return ()

    class FakeWriter:
        def __init__(self, *_args):
            pass

        def encode(self, _item):
            return b"wire", "169.254.1.1"

    monkeypatch.setattr(za_client, "ZASession", lambda **_kwargs: object())
    monkeypatch.setattr(za_client, "ReliableTradeEngine", FakeTrade)
    monkeypatch.setattr(za_client, "ZATradeClient", FakeClient)
    monkeypatch.setattr(za_client, "PiaCrypto", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(za_client, "_LivePacketWriter", FakeWriter)
    monkeypatch.setattr(za_client, "LiveTransport", FakeTransport)


def _occupied_party(species: int = 25) -> PA9:
    record = PA9()
    record.species_internal = species
    raw = bytearray(record.to_bytes(party=True))
    # Party-tail contents are excluded from the stored checksum.
    raw[0x148] = 50
    raw[0x14A:0x14C] = (100).to_bytes(2, "little")
    for offset in range(0x14C, 0x156, 2):
        raw[offset : offset + 2] = (80).to_bytes(2, "little")
    return PA9(raw)


def test_uses_recovered_protocol_specific_and_emits_initial_ready_then_ack() -> None:
    engine = ReliableTradeEngine()
    engine.feed_message(_reliable_message(_application(_ready())), now=0)
    output = engine.drain(now=0)
    assert len(output) == 2

    ready = parse_reliable(output[0].payload)
    assert ready.sequence_id == 0xFFF0
    assert ready.flags == 0x0F
    assert ready.payload[:2] == b"\x01\x00"
    assert output[0].protocol_specific == STREAM
    assert output[0].message_flags == DATA_MESSAGE_FLAGS
    compact = build_message(
        10,
        output[0].payload,
        output[0].message_flags,
        port=output[0].port,
        protocol_specific=output[0].protocol_specific,
        force_full_header=True,
    )
    assert compact[0] == 0x7F
    assert compact[1] == 0x20
    assert compact[4:7] == bytes((10, 0, STREAM))

    ack = parse_reliable(output[1].payload)
    assert ack.flags == 0
    assert ack.sequence_id == 0xFFF0
    assert ack.lowest_pending_ack == 0xFFF0
    assert ack.destination_bit_count == 0
    assert len(output[1].payload) == 28
    assert output[1].message_flags == ACK_MESSAGE_FLAGS
    parsed_ack = parse_bulk_ack(ack.payload)
    assert parsed_ack.ack_flags == BULK_ACK_FLAGS
    assert parsed_ack.entries[0].next_expected == 0xFFF1
    ack_compact = build_message(
        10,
        output[1].payload,
        output[1].message_flags,
        port=output[1].port,
        protocol_specific=output[1].protocol_specific,
    )
    assert ack_compact[:2] == b"\x1f\x40"
    assert ack_compact[4:7] == bytes((10, 0, STREAM))


def test_first_delta_message_inherits_native_port_and_protocol_specific() -> None:
    frame = _application(_ready()).pack()
    message = parse_messages(
        b"\x07\x20" + len(frame).to_bytes(2, "big") + b"\x0a" + frame
    )[0]
    engine = ReliableTradeEngine()
    engine.feed_message(message, now=0)
    assert engine.negotiation.remote_ready is True


def test_wrong_protocol_specific_id_is_rejected() -> None:
    frame = _application(_ready()).pack()
    message = PiaMessage(
        FIELDS, DATA_MESSAGE_FLAGS, len(frame), 10, 0, 0, frame
    )
    with pytest.raises(ClientProtocolError, match="must be 0xFD"):
        ReliableTradeEngine().feed_message(message, now=0)


def test_later_wrong_protocol_specific_id_is_rejected() -> None:
    engine = ReliableTradeEngine()
    engine.feed_message(_reliable_message(_application(_ready())), now=0)
    with pytest.raises(ClientProtocolError, match="must be 0xFD"):
        engine.feed_message(
            _reliable_message(_application(_ready(), 0xFFF1), stream=STREAM + 1),
            now=0.1,
        )


def test_ack_releases_send_window() -> None:
    engine = ReliableTradeEngine()
    engine.feed_message(_reliable_message(_application(_ready())), now=0)
    engine.drain(now=0)
    assert engine.unacknowledged == (0xFFF0,)

    control = ReliableFrame(
        0,
        0xFFF0,
        0xFFF0,
        build_bulk_ack(0xFFF1, ack_flags=BULK_ACK_FLAGS),
    )
    engine.feed_message(_reliable_message(control), now=0.1)
    assert engine.unacknowledged == ()


def test_retransmission_preserves_sequence_and_marks_output() -> None:
    engine = ReliableTradeEngine(retransmit_timeout=0.5)
    engine.feed_message(_reliable_message(_application(_ready())), now=0)
    first = engine.drain(now=0)[0]
    assert engine.drain(now=0.49) == ()
    retried = engine.drain(now=0.5)
    assert len(retried) == 1
    assert retried[0].retransmission is True
    assert parse_reliable(retried[0].payload).sequence_id == parse_reliable(first.payload).sequence_id


def test_retry_budget_fails_closed() -> None:
    engine = ReliableTradeEngine(retransmit_timeout=0.1, max_attempts=1)
    engine.feed_message(_reliable_message(_application(_ready())), now=0)
    engine.drain(now=0)
    with pytest.raises(ReliableTimeout, match="not acknowledged"):
        engine.drain(now=0.1)


def test_out_of_order_frame_produces_selective_ack() -> None:
    engine = ReliableTradeEngine()
    engine.feed_message(
        _reliable_message(_application(_ready(), sequence=0xFFF1)), now=0
    )
    output = engine.drain(now=0)
    ack = parse_bulk_ack(parse_reliable(output[-1].payload).payload)
    assert ack.entries[0].next_expected == 0xFFF0
    assert ack.entries[0].mask[0] & 1


def test_send_sequence_wraps_and_subsequent_data_uses_07() -> None:
    offer = bytes(0x158)
    engine = ReliableTradeEngine(offer=offer, initial_sequence=0xFFFF)
    engine.feed_message(
        _reliable_message(_application(_ready(), sequence=0xFFFF)), now=0
    )
    data = [
        parse_reliable(item.payload)
        for item in engine.drain(now=0)
        if item.message_flags == DATA_MESSAGE_FLAGS
    ]
    assert [frame.sequence_id for frame in data] == [0xFFFF, 0]
    assert [int(frame.flags) for frame in data] == [0x0F, 0x07]
    assert engine.send_next == 1


def test_auto_confirm_requires_explicit_opt_in() -> None:
    offer = bytes(0x158)
    engine = ReliableTradeEngine(offer=offer, auto_confirm=False)
    engine.feed_message(_reliable_message(_application(_ready())), now=0)
    engine.drain(now=0)
    remote_select = _application(SelectPokemon(0, offer), sequence=0xFFF1)
    engine.feed_message(_reliable_message(remote_select), now=0.1)
    assert engine.negotiation.local_state is TradeState.OFFERED
    assert not any(
        isinstance(action.command, ConfirmTrade)
        for action in engine.negotiation.pending_actions
    )

    automatic = ReliableTradeEngine(offer=offer, auto_confirm=True)
    automatic.feed_message(_reliable_message(_application(_ready())), now=0)
    automatic.drain(now=0)
    automatic.feed_message(_reliable_message(remote_select), now=0.1)
    data = [
        parse_reliable(item.payload).payload[:2]
        for item in automatic.drain(now=0.1)
        if item.message_flags == DATA_MESSAGE_FLAGS
    ]
    assert b"\x01\x02" in data


def test_transient_select_cannot_replace_active_adapter_offer() -> None:
    committed = bytes([0x55]) * 0x158
    transient = bytes([0xAA]) * 0x158
    engine = ReliableTradeEngine()
    engine.feed_message(_reliable_message(_application(_ready())), now=0)
    engine.drain(now=0)

    engine.feed_message(
        _reliable_message(
            _application(SelectPokemon(0, committed), sequence=0xFFF1)
        ),
        now=0.1,
    )
    engine.feed_message(
        _reliable_message(
            _application(SelectPokemon(0, transient, True), sequence=0xFFF2)
        ),
        now=0.2,
    )

    assert engine.negotiation.remote_offer == transient
    assert engine.remote_offer == committed


def test_non_trade_reliable_port_is_ignored() -> None:
    frame = _application(_ready()).pack()
    message = PiaMessage(FIELDS, 0, len(frame), 10, 3, STREAM, frame)
    engine = ReliableTradeEngine()
    engine.feed_message(message, now=0)
    assert engine.negotiation.remote_ready is False


def test_other_application_endpoint_is_acked_but_not_decoded_as_trade() -> None:
    foreign = ReliableFrame(
        ReliableFlags.APPLICATION_DATA
        | ReliableFlags.MESSAGE_START
        | ReliableFlags.MESSAGE_END
        | ReliableFlags.INITIALIZED,
        0xFFF0,
        0xFFF0,
        b"\x02\x00foreign endpoint",
    )
    engine = ReliableTradeEngine()
    engine.feed_message(_reliable_message(foreign), now=0)
    output = engine.drain(now=0)
    assert list(engine.received_commands) == []
    assert parse_bulk_ack(parse_reliable(output[-1].payload).payload).entries[
        0
    ].next_expected == 0xFFF1


def test_received_command_diagnostics_are_bounded() -> None:
    engine = ReliableTradeEngine()
    for value in range(MAX_RECEIVED_COMMAND_HISTORY + 5):
        engine.received_commands.append(value)  # type: ignore[arg-type]
    assert len(engine.received_commands) == MAX_RECEIVED_COMMAND_HISTORY
    assert engine.received_commands[0] == 5


def test_reliable_reset_fails_closed_instead_of_being_misparsed_as_ack() -> None:
    reset = ReliableFrame(ReliableFlags.RESET, 0xFFF0, 0xFFF0)
    engine = ReliableTradeEngine()
    with pytest.raises(ClientProtocolError, match="reset negotiation"):
        engine.feed_message(_reliable_message(reset), now=0)


def test_live_offer_requires_real_party_tail() -> None:
    party = _occupied_party()
    encoded = prepare_party_offer(party)
    assert len(encoded) == 0x158
    stored = party.to_bytes(party=False)
    with pytest.raises(ValueError, match="cannot be safely widened"):
        prepare_party_offer(stored)
    with pytest.raises(ValueError, match="all-zero party-stat tail"):
        prepare_party_offer(stored + bytes(0x10))
    encrypted_stored = party.to_bytes(encrypted=True, party=False)
    with pytest.raises(ValueError, match="all-zero"):
        prepare_party_offer(encrypted_stored + bytes(0x10))
    with pytest.raises(ValueError, match="empty PA9"):
        prepare_party_offer(PA9())


def test_live_offer_path_is_size_checked_without_unbounded_read(
    tmp_path, monkeypatch
) -> None:
    oversized = tmp_path / "oversized.pa9"
    oversized.write_bytes(bytes(PA9.PARTY_SIZE + 1))

    def unbounded_read_forbidden(_path):
        pytest.fail("prepare_party_offer must not call Path.read_bytes")

    monkeypatch.setattr(type(oversized), "read_bytes", unbounded_read_forbidden)
    with pytest.raises(ValueError, match="exactly 0x158 bytes"):
        prepare_party_offer(oversized)


def test_live_offer_path_rejects_non_regular_files(tmp_path) -> None:
    with pytest.raises(ValueError, match="not a regular file"):
        prepare_party_offer(tmp_path)


@pytest.mark.parametrize("alias_kind", ["same-path", "hard-link"])
def test_live_trade_never_replaces_its_offer_even_with_force(
    alias_kind, tmp_path
) -> None:
    offer = tmp_path / "offer.epa9"
    original = _occupied_party().to_bytes(encrypted=True)
    offer.write_bytes(original)
    output = offer
    if alias_kind == "hard-link":
        output = tmp_path / "received.epa9"
        output.hardlink_to(offer)

    def unexpected_transport():
        pytest.fail("alias rejection must happen before transport construction")

    with pytest.raises(ValueError, match="offer and output"):
        za_client.run_live_trade(
            offer,
            output=output,
            overwrite=True,
            auto_confirm=True,
            transport_factory=unexpected_transport,
        )

    assert offer.read_bytes() == original
    assert output.read_bytes() == original


def test_live_trade_rejects_output_changed_after_gui_approval(tmp_path) -> None:
    target = tmp_path / "received.epa9"
    target.write_bytes(b"approved version")
    approved_identity = za_client._target_identity(target)
    target.write_bytes(b"new version from another process")

    def unexpected_transport():
        pytest.fail("identity rejection must happen before transport construction")

    with pytest.raises(FileExistsError, match="changed after approval"):
        za_client.run_live_trade(
            _occupied_party(),
            output=target,
            overwrite=True,
            expected_output_identity=approved_identity,
            auto_confirm=True,
            transport_factory=unexpected_transport,
        )

    assert target.read_bytes() == b"new version from another process"


def test_received_party_save_is_atomic_and_preserves_existing_file_on_failure(
    monkeypatch, tmp_path
) -> None:
    received = _occupied_party().to_bytes(encrypted=True)
    target = tmp_path / "received.epa9"
    target.write_bytes(b"existing")

    def fail_replace(_source, _target):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(za_client.os, "replace", fail_replace)
    approved = za_client._preflight_trade_output(target, overwrite=True)
    with pytest.raises(OSError, match="synthetic"):
        za_client._atomic_save_received_party(received, target, expected_target=approved)

    assert target.read_bytes() == b"existing"
    assert list(tmp_path.glob(".received.epa9.*.tmp")) == []


def test_windows_live_trade_requires_and_selects_relay(monkeypatch) -> None:
    captured = {}
    stopped = []

    class RelayStarted(Exception):
        pass

    class FakeRelay:
        def __init__(self, endpoint, **kwargs):
            captured["endpoint"] = endpoint
            captured.update(kwargs)

        def start(self):
            raise RelayStarted

        def stop(self):
            stopped.append(True)

    monkeypatch.setattr(za_client, "os", SimpleNamespace(name="nt"))
    with pytest.raises(RuntimeError, match="--relay"):
        za_client.run_live_trade(
            _occupied_party(), auto_confirm=True, link_code="12345678"
        )

    monkeypatch.setattr(za_client, "RelayTransport", FakeRelay)
    with pytest.raises(RelayStarted):
        za_client.run_live_trade(
            _occupied_party(),
            auto_confirm=True,
            keys_path=None,
            relay="127.0.0.1:24872",
            link_code="12345678",
        )
    assert captured["endpoint"] == "127.0.0.1:24872"
    assert captured["communication_id"] == int("0100F43008C44000", 16)
    assert captured["expected_max_participants"] == 2
    assert captured["link_code"] == "12345678"
    assert stopped == [True]


def test_live_trade_revalidates_joined_link_code_before_title_traffic() -> None:
    events = []

    class WrongRoomTransport:
        app_data = (
            b"\x00\x5c\x16\x00\x06"
            + bytes(0x5C - 5)
            + b"87654321"
            + bytes(8)
            + (8).to_bytes(4, "little")
        )

        def start(self):
            events.append("start")

        def stop(self):
            events.append("stop")

    with pytest.raises(za_client.ClientProtocolError, match="requested Link Code"):
        za_client.run_live_trade(
            _occupied_party(),
            auto_confirm=True,
            link_code="12345678",
            transport_factory=WrongRoomTransport,
        )

    assert events == ["start", "stop"]


def test_live_trade_honors_preflight_cancellation_before_transport() -> None:
    cancelled = threading.Event()
    cancelled.set()
    created = []

    with pytest.raises(za_client.OperationCancelled):
        za_client.run_live_trade(
            _occupied_party(),
            auto_confirm=True,
            cancel_event=cancelled,
            transport_factory=lambda: created.append(True),  # type: ignore[arg-type]
        )
    assert created == []


def test_live_confirmation_wait_keeps_protocol_pumping(monkeypatch) -> None:
    callback_entered = threading.Event()
    allow_decision = threading.Event()
    remote_offer = _occupied_party().to_bytes(encrypted=True)
    events = []

    class FakeTransport:
        def __init__(self):
            self.app_data = b"\x00\x5c\x16\x00\x01" + bytes(0x5C - 5)
            self.our_mac = b"\x10" * 6
            self.host_mac = b"\x20" * 6
            self.our_ip = "169.254.1.2"
            self.host_ip = "169.254.1.1"
            self.broadcast = "169.254.1.255"
            self.ssid = bytes(16)

        def start(self):
            events.append("start")

        def recv(self):
            return []

        def send(self, _datagram, _destination):
            events.append("send")

        def stop(self):
            events.append("stop")

    class FakeTrade:
        instance = None

        def __init__(self, **_kwargs):
            type(self).instance = self
            self.remote_offer = remote_offer
            self.negotiation = type(
                "Negotiation",
                (),
                {
                    "local_state": TradeState.OFFERED,
                    "remote_state": TradeState.OFFERED,
                    "remote_cancel_count": 0,
                },
            )()
            self.complete = False
            self.unacknowledged = ()
            self.confirmed = False

        def confirm(self, *, now):
            self.confirmed = True
            self.complete = True

    class FakeClient:
        instance = None

        def __init__(self, _session, _trade):
            type(self).instance = self
            self.drains = 0
            self.trade = _trade
            self.emitted = False

        def feed_decoded(self, *_args, **_kwargs):
            raise AssertionError("no incoming packet expected")

        def drain(self, *, now, tick):
            self.drains += 1
            if self.drains >= 3:
                allow_decision.set()
            if self.trade.confirmed and not self.emitted:
                self.emitted = True
                return (_synthetic_trade_outbound(FinalAgreement(0)),)
            return ()

    monkeypatch.setattr(za_client, "ZASession", lambda **_kwargs: object())
    monkeypatch.setattr(za_client, "ReliableTradeEngine", FakeTrade)
    monkeypatch.setattr(za_client, "ZATradeClient", FakeClient)
    monkeypatch.setattr(za_client, "PiaCrypto", lambda *_args, **_kwargs: object())
    class FakeWriter:
        def __init__(self, *_args):
            pass

        def encode(self, _item):
            return b"synthetic-final", "169.254.1.1"

    monkeypatch.setattr(za_client, "_LivePacketWriter", FakeWriter)

    def confirm(_offer: bytes) -> bool:
        callback_entered.set()
        assert allow_decision.wait(1)
        return True

    result = za_client.run_live_trade(
        _occupied_party(),
        confirmation_callback=confirm,
        settle_grace=0,
        timeout=2,
        transport_factory=FakeTransport,
    )

    assert callback_entered.is_set()
    assert FakeClient.instance.drains >= 3
    assert FakeTrade.instance.confirmed
    assert result.received_party_pa9 == remote_offer
    assert events == ["start", "send", "stop"]


def test_live_decline_sends_cancel_once_then_cleans_up(monkeypatch) -> None:
    events = []

    class FakeTransport:
        def __init__(self, **kwargs):
            self.app_data = b"\x00\x5c\x16\x00\x01" + bytes(0x5C - 5)
            self.our_mac = b"\x10" * 6
            self.host_mac = b"\x20" * 6
            self.our_ip = "169.254.1.2"
            self.host_ip = "169.254.1.1"
            self.broadcast = "169.254.1.255"
            self.ssid = bytes(16)

        def start(self):
            events.append("start")

        def recv(self):
            return []

        def send(self, datagram, destination):
            events.append(("send", datagram, destination))

        def stop(self):
            events.append("stop")

    class FakeSession:
        def __init__(self, **kwargs):
            pass

    class FakeTrade:
        instance = None

        def __init__(self, **kwargs):
            type(self).instance = self
            self.remote_offer = _occupied_party().to_bytes(encrypted=True)
            self.negotiation = type(
                "Negotiation",
                (),
                {
                    "local_state": TradeState.OFFERED,
                    "remote_state": TradeState.OFFERED,
                    "remote_cancel_count": 0,
                },
            )()
            self.complete = False
            self.cancelled = False
            self.cancel_reason = None
            self.cancel_acked = False
            self.offer_cleared = False

        @property
        def unacknowledged(self):
            return (1,) if self.cancelled and not self.cancel_acked else ()

        def set_offer(self, value):
            self.offer_cleared = value is None

        def cancel(self, reason=0, *, now):
            self.cancelled = True
            self.cancel_reason = reason

    class FakeClient:
        def __init__(self, session, trade):
            self.trade = trade
            self.cancel_drains = 0

        def feed_decoded(self, decoded, *, now, tick):
            raise AssertionError("no incoming datagrams expected")

        def drain(self, *, now, tick):
            if self.trade.cancelled:
                self.cancel_drains += 1
                if self.cancel_drains == 1:
                    return (_synthetic_trade_outbound(ConfirmTrade(0)),)
                self.trade.cancel_acked = True
            return ()

    class FakeWriter:
        def __init__(self, transport, crypto):
            pass

        def encode(self, item):
            return b"cancel-datagram", "169.254.1.1"

    monkeypatch.setattr(za_client, "LiveTransport", FakeTransport)
    monkeypatch.setattr(za_client, "ZASession", FakeSession)
    monkeypatch.setattr(za_client, "ReliableTradeEngine", FakeTrade)
    monkeypatch.setattr(za_client, "ZATradeClient", FakeClient)
    monkeypatch.setattr(za_client, "PiaCrypto", lambda *args, **kwargs: object())
    monkeypatch.setattr(za_client, "_LivePacketWriter", FakeWriter)

    with pytest.raises(za_client.TradeDeclined):
        za_client.run_live_trade(
            _occupied_party(),
            confirmation_callback=lambda offer: False,
            transport_factory=FakeTransport,
        )

    assert FakeTrade.instance.cancelled
    assert FakeTrade.instance.cancel_reason == 0
    assert FakeTrade.instance.offer_cleared
    assert events == [
        "start",
        ("send", b"cancel-datagram", "169.254.1.1"),
        "stop",
    ]


def test_live_auto_confirm_rejects_malformed_remote_pa9_before_send(
    monkeypatch,
) -> None:
    events = []

    class FakeTransport:
        def __init__(self, **kwargs):
            self.app_data = b"\x00\x5c\x16\x00\x01" + bytes(0x5C - 5)
            self.our_mac = b"\x10" * 6
            self.host_mac = b"\x20" * 6
            self.our_ip = "169.254.1.2"
            self.host_ip = "169.254.1.1"
            self.broadcast = "169.254.1.255"
            self.ssid = bytes(16)

        def start(self):
            events.append("start")

        def recv(self):
            return []

        def send(self, datagram, destination):
            events.append(("unexpected-send", datagram, destination))

        def stop(self):
            events.append("stop")

    class FakeTrade:
        instance = None

        def __init__(self, **kwargs):
            type(self).instance = self
            assert kwargs["auto_confirm"] is False
            self.remote_offer = bytes(0x158)
            self.negotiation = type(
                "Negotiation",
                (),
                {
                    "local_state": TradeState.OFFERED,
                    "remote_state": TradeState.OFFERED,
                    "remote_cancel_count": 0,
                },
            )()
            self.complete = False
            self.unacknowledged = ()
            self.confirmed = False

        def confirm(self, *, now):
            self.confirmed = True

    class FakeClient:
        def __init__(self, session, trade):
            pass

        def feed_decoded(self, decoded, *, now, tick):
            raise AssertionError("no incoming datagrams expected")

        def drain(self, *, now, tick):
            raise AssertionError("malformed offer must fail before draining Confirm")

    monkeypatch.setattr(za_client, "LiveTransport", FakeTransport)
    monkeypatch.setattr(za_client, "ZASession", lambda **kwargs: object())
    monkeypatch.setattr(za_client, "ReliableTradeEngine", FakeTrade)
    monkeypatch.setattr(za_client, "ZATradeClient", FakeClient)
    monkeypatch.setattr(za_client, "PiaCrypto", lambda *args, **kwargs: object())
    monkeypatch.setattr(za_client, "_LivePacketWriter", lambda *args: object())

    with pytest.raises(ClientProtocolError, match="all-zero encrypted party tail"):
        za_client.run_live_trade(
            _occupied_party(), auto_confirm=True, transport_factory=FakeTransport
        )

    assert FakeTrade.instance.confirmed is False
    assert events == ["start", "stop"]


def test_live_rejects_committed_offer_change_after_confirmation(
    monkeypatch,
) -> None:
    events = []
    approved = _occupied_party(25).to_bytes(encrypted=True)
    replacement = _occupied_party(26).to_bytes(encrypted=True)

    class FakeTransport:
        def __init__(self, **kwargs):
            self.app_data = b"\x00\x5c\x16\x00\x01" + bytes(0x5C - 5)
            self.our_mac = b"\x10" * 6
            self.host_mac = b"\x20" * 6
            self.our_ip = "169.254.1.2"
            self.host_ip = "169.254.1.1"
            self.broadcast = "169.254.1.255"
            self.ssid = bytes(16)

        def start(self):
            events.append("start")

        def recv(self):
            return []

        def send(self, datagram, destination):
            events.append(("unexpected-send", datagram, destination))

        def stop(self):
            events.append("stop")

    class FakeTrade:
        instance = None

        def __init__(self, **kwargs):
            type(self).instance = self
            self.remote_offer = approved
            self.negotiation = type(
                "Negotiation",
                (),
                {
                    "local_state": TradeState.OFFERED,
                    "remote_state": TradeState.OFFERED,
                    "remote_cancel_count": 0,
                },
            )()
            self.complete = False
            self.unacknowledged = ()
            self.confirmed = False

        def confirm(self, *, now):
            self.confirmed = True

    class FakeClient:
        def __init__(self, session, trade):
            self.trade = trade

        def feed_decoded(self, decoded, *, now, tick):
            raise AssertionError("no incoming datagrams expected")

        def drain(self, *, now, tick):
            if self.trade.confirmed:
                self.trade.remote_offer = replacement
            return ()

    monkeypatch.setattr(za_client, "LiveTransport", FakeTransport)
    monkeypatch.setattr(za_client, "ZASession", lambda **kwargs: object())
    monkeypatch.setattr(za_client, "ReliableTradeEngine", FakeTrade)
    monkeypatch.setattr(za_client, "ZATradeClient", FakeClient)
    monkeypatch.setattr(za_client, "PiaCrypto", lambda *args, **kwargs: object())
    monkeypatch.setattr(za_client, "_LivePacketWriter", lambda *args: object())

    seen = []

    def approve(offer):
        seen.append(offer)
        return True

    with pytest.raises(ClientProtocolError, match="changed its committed PA9"):
        za_client.run_live_trade(
            _occupied_party(),
            confirmation_callback=approve,
            transport_factory=FakeTransport,
        )

    assert seen == [approved]
    assert FakeTrade.instance.confirmed is True
    assert events == ["start", "stop"]


@pytest.mark.parametrize(
    ("target_kind", "error_type", "message"),
    [
        ("missing-parent", ValueError, "output directory does not exist"),
        ("directory", ValueError, "not a regular file"),
        ("existing-file", FileExistsError, "already exists"),
    ],
)
def test_live_output_preflight_fails_before_transport_creation(
    tmp_path, target_kind, error_type, message
) -> None:
    if target_kind == "missing-parent":
        target = tmp_path / "missing" / "received.epa9"
    else:
        target = tmp_path / "received.epa9"
        if target_kind == "directory":
            target.mkdir()
        else:
            target.write_bytes(b"existing")

    created = []
    with pytest.raises(error_type, match=message):
        za_client.run_live_trade(
            _occupied_party(),
            output=target,
            auto_confirm=True,
            transport_factory=lambda: created.append(True),  # type: ignore[arg-type]
        )

    assert created == []


@pytest.mark.parametrize("terminal_event", ["cancel", "timeout"])
def test_live_trade_persists_after_local_final_agreement(
    monkeypatch, tmp_path, terminal_event
) -> None:
    remote_offer = _occupied_party(133).to_bytes(encrypted=True)
    cancelled = threading.Event()
    events = []

    class FakeTransport:
        def __init__(self):
            self.app_data = b"\x00\x5c\x16\x00\x01" + bytes(0x5C - 5)
            self.our_mac = b"\x10" * 6
            self.host_mac = b"\x20" * 6
            self.our_ip = "169.254.1.2"
            self.host_ip = "169.254.1.1"
            self.broadcast = "169.254.1.255"
            self.ssid = bytes(16)

        def start(self):
            events.append("start")

        def recv(self):
            return []

        def send(self, datagram, destination):
            events.append(("send", datagram, destination))
            if terminal_event == "cancel":
                cancelled.set()

        def stop(self):
            events.append("stop")

    class FakeTrade:
        instance = None

        def __init__(self, **_kwargs):
            type(self).instance = self
            self.remote_offer = remote_offer
            self.negotiation = SimpleNamespace(
                local_state=TradeState.OFFERED,
                remote_state=TradeState.OFFERED,
                remote_cancel_count=0,
            )
            self.complete = False
            self.unacknowledged = ()
            self.confirmed = False

        def confirm(self, *, now):
            self.confirmed = True
            self.negotiation.local_state = TradeState.FINAL_AGREEMENT

    class FakeClient:
        def __init__(self, _session, trade):
            self.trade = trade
            self.final_emitted = False

        def feed_decoded(self, *_args, **_kwargs):
            raise AssertionError("no incoming packet expected")

        def drain(self, *, now, tick):
            if self.trade.confirmed and not self.final_emitted:
                self.final_emitted = True
                return (
                    SimpleNamespace(
                        protocol=10,
                        protocol_specific=STREAM,
                        payload=_application(FinalAgreement(1)).pack(),
                    ),
                )
            return ()

    class FakeWriter:
        def __init__(self, *_args):
            pass

        def encode(self, _item):
            return b"final-agreement", "169.254.1.1"

    monkeypatch.setattr(za_client, "ZASession", lambda **_kwargs: object())
    monkeypatch.setattr(za_client, "ReliableTradeEngine", FakeTrade)
    monkeypatch.setattr(za_client, "ZATradeClient", FakeClient)
    monkeypatch.setattr(za_client, "PiaCrypto", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(za_client, "_LivePacketWriter", FakeWriter)

    timeout = 2.0
    if terminal_event == "timeout":
        current = [-0.01]

        def monotonic() -> float:
            current[0] += 0.01
            return current[0]

        monkeypatch.setattr(za_client.time, "monotonic", monotonic)
        monkeypatch.setattr(za_client.time, "sleep", lambda _seconds: None)
        timeout = 0.025

    target = tmp_path / f"received-{terminal_event}.epa9"
    result = za_client.run_live_trade(
        _occupied_party(),
        output=target,
        auto_confirm=True,
        timeout=timeout,
        settle_grace=0,
        cancel_event=cancelled,
        transport_factory=FakeTransport,
    )

    assert result.completed is False
    assert result.warning is not None
    assert ("stop" if terminal_event == "cancel" else "timeout") in result.warning
    assert result.output_path == target
    assert PA9(target.read_bytes(), encrypted=True).species_internal == 133
    assert events == [
        "start",
        ("send", b"final-agreement", "169.254.1.1"),
        "stop",
    ]


def test_live_cancel_after_final_queue_wins_before_network_send(
    monkeypatch, tmp_path
) -> None:
    cancelled = threading.Event()
    events: list[object] = []
    target = tmp_path / "must-not-exist.epa9"

    def outbound_factory():
        cancelled.set()
        return (_synthetic_trade_outbound(FinalAgreement(2)),)

    _install_live_boundary_fakes(
        monkeypatch,
        events=events,
        outbound_factory=outbound_factory,
        send_callback=lambda: None,
    )

    with pytest.raises(za_client.OperationCancelled):
        za_client.run_live_trade(
            _occupied_party(),
            output=target,
            auto_confirm=True,
            cancel_event=cancelled,
            transport_factory=za_client.LiveTransport,
        )

    assert events == ["start", "stop"]
    assert not target.exists()


def test_live_nonfinal_send_failure_before_final_is_not_committed(
    monkeypatch, tmp_path
) -> None:
    events: list[object] = []
    target = tmp_path / "must-not-be-saved.epa9"

    def fail_send():
        raise RuntimeError("synthetic precommit send failure")

    _install_live_boundary_fakes(
        monkeypatch,
        events=events,
        outbound_factory=lambda: (
            _synthetic_trade_outbound(ConfirmTrade(2)),
            _synthetic_trade_outbound(FinalAgreement(2)),
        ),
        send_callback=fail_send,
    )

    with pytest.raises(RuntimeError, match="precommit send failure"):
        za_client.run_live_trade(
            _occupied_party(),
            output=target,
            auto_confirm=True,
            transport_factory=za_client.LiveTransport,
        )

    assert events == ["start", ("send", b"wire", "169.254.1.1"), "stop"]
    assert not target.exists()


def test_live_final_send_failure_is_incomplete_even_after_remote_final(
    monkeypatch, tmp_path
) -> None:
    events: list[object] = []
    target = tmp_path / "preserved-after-send-error.epa9"

    def fail_send():
        raise RuntimeError("synthetic ambiguous final send failure")

    _install_live_boundary_fakes(
        monkeypatch,
        events=events,
        outbound_factory=lambda: (
            _synthetic_trade_outbound(FinalAgreement(2)),
        ),
        send_callback=fail_send,
        trade_complete=True,
    )

    result = za_client.run_live_trade(
        _occupied_party(),
        output=target,
        auto_confirm=True,
        transport_factory=za_client.LiveTransport,
    )

    assert result.completed is False
    assert result.warning is not None and "RuntimeError" in result.warning
    assert target.exists()
    assert PA9(target.read_bytes(), encrypted=True).species_internal == 133
    assert events == ["start", ("send", b"wire", "169.254.1.1"), "stop"]


def test_postcommit_reliable_timeout_is_incomplete_after_remote_final(
    monkeypatch, tmp_path
) -> None:
    events: list[object] = []
    target = tmp_path / "preserved-after-retry-timeout.epa9"

    _install_live_boundary_fakes(
        monkeypatch,
        events=events,
        outbound_factory=lambda: (
            _synthetic_trade_outbound(FinalAgreement(2)),
        ),
        send_callback=lambda: None,
        trade_complete=True,
        trade_unacknowledged=(INITIAL_RELIABLE_SEQUENCE,),
        drain_after_emit_error=ReliableTimeout("synthetic retry budget exhausted"),
    )

    result = za_client.run_live_trade(
        _occupied_party(),
        output=target,
        auto_confirm=True,
        transport_factory=za_client.LiveTransport,
    )

    assert result.completed is False
    assert result.warning is not None and "ReliableTimeout" in result.warning
    assert PA9(target.read_bytes(), encrypted=True).species_internal == 133
    assert events == ["start", ("send", b"wire", "169.254.1.1"), "stop"]


def test_postcommit_timeout_is_incomplete_while_final_is_unacknowledged(
    monkeypatch, tmp_path
) -> None:
    events: list[object] = []
    target = tmp_path / "preserved-after-overall-timeout.epa9"
    clock = [0.0]

    monkeypatch.setattr(za_client.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        za_client.time,
        "sleep",
        lambda _seconds: clock.__setitem__(0, clock[0] + 0.001),
    )

    _install_live_boundary_fakes(
        monkeypatch,
        events=events,
        outbound_factory=lambda: (
            _synthetic_trade_outbound(FinalAgreement(2)),
        ),
        send_callback=lambda: clock.__setitem__(0, 1.0),
        trade_complete=True,
        trade_unacknowledged=(INITIAL_RELIABLE_SEQUENCE,),
    )

    result = za_client.run_live_trade(
        _occupied_party(),
        output=target,
        auto_confirm=True,
        timeout=0.5,
        settle_grace=0,
        transport_factory=za_client.LiveTransport,
    )

    assert result.completed is False
    assert result.warning is not None and "acknowledgement" in result.warning
    assert PA9(target.read_bytes(), encrypted=True).species_internal == 133
    assert events == ["start", ("send", b"wire", "169.254.1.1"), "stop"]


def test_stop_during_postcommit_settle_cannot_return_completed(
    monkeypatch, tmp_path
) -> None:
    cancelled = threading.Event()
    events: list[object] = []
    target = tmp_path / "preserved-after-stop-during-settle.epa9"
    clock = [0.0]

    monkeypatch.setattr(za_client.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        za_client.time,
        "sleep",
        lambda _seconds: clock.__setitem__(0, clock[0] + 0.04),
    )

    _install_live_boundary_fakes(
        monkeypatch,
        events=events,
        outbound_factory=lambda: (
            _synthetic_trade_outbound(FinalAgreement(2)),
        ),
        send_callback=cancelled.set,
        trade_complete=True,
    )

    result = za_client.run_live_trade(
        _occupied_party(),
        output=target,
        auto_confirm=True,
        timeout=0.5,
        settle_grace=0.03,
        cancel_event=cancelled,
        transport_factory=za_client.LiveTransport,
    )

    assert result.completed is False
    assert result.warning is not None and "stop was requested" in result.warning
    assert PA9(target.read_bytes(), encrypted=True).species_internal == 133
    assert events == ["start", ("send", b"wire", "169.254.1.1"), "stop"]


def test_final_retransmission_allows_updated_lowest_pending_ack(
    monkeypatch, tmp_path
) -> None:
    cancelled = threading.Event()
    events: list[object] = []
    sends = 0
    target = tmp_path / "preserved-after-final-retry.epa9"
    application = _application(FinalAgreement(2), sequence=0xFFF1)

    def outbound(lowest_pending_ack: int):
        reliable = ReliableFrame(
            application.flags,
            application.sequence_id,
            lowest_pending_ack,
            application.payload,
        )
        return SimpleNamespace(
            protocol=10,
            protocol_specific=STREAM,
            payload=reliable.pack(),
        )

    def cancel_after_retry() -> None:
        nonlocal sends
        sends += 1
        if sends == 2:
            cancelled.set()

    _install_live_boundary_fakes(
        monkeypatch,
        events=events,
        outbound_factory=lambda: (
            outbound(INITIAL_RELIABLE_SEQUENCE),
            outbound(0xFFF1),
        ),
        send_callback=cancel_after_retry,
    )

    result = za_client.run_live_trade(
        _occupied_party(),
        output=target,
        auto_confirm=True,
        timeout=0.2,
        settle_grace=0,
        cancel_event=cancelled,
        transport_factory=za_client.LiveTransport,
    )

    assert result.completed is False
    assert sends == 2
    assert events == [
        "start",
        ("send", b"wire", "169.254.1.1"),
        ("send", b"wire", "169.254.1.1"),
        "stop",
    ]


def test_boundary_logger_failure_cannot_skip_final_network_send(
    monkeypatch, tmp_path
) -> None:
    events: list[object] = []
    target = tmp_path / "preserved-with-broken-log.epa9"

    def fail_send():
        raise RuntimeError("synthetic ambiguous final send failure")

    def broken_log(_message):
        raise BrokenPipeError("synthetic closed diagnostic stream")

    _install_live_boundary_fakes(
        monkeypatch,
        events=events,
        outbound_factory=lambda: (
            _synthetic_trade_outbound(FinalAgreement(2)),
        ),
        send_callback=fail_send,
    )

    result = za_client.run_live_trade(
        _occupied_party(),
        output=target,
        auto_confirm=True,
        transport_factory=za_client.LiveTransport,
        log=broken_log,
    )

    assert result.completed is False
    assert target.exists()
    assert events == ["start", ("send", b"wire", "169.254.1.1"), "stop"]


def test_fsynced_write_ahead_pa9_exists_during_final_send_attempt(
    monkeypatch, tmp_path
) -> None:
    events: list[object] = []
    target = tmp_path / "crash-proof.epa9"
    pending_seen = []

    def observe_stage_then_fail():
        pending = list(tmp_path.glob(".crash-proof.pending-*.epa9"))
        assert len(pending) == 1
        assert not target.exists()
        assert PA9(pending[0].read_bytes(), encrypted=True).species_internal == 133
        pending_seen.extend(pending)
        raise RuntimeError("synthetic ambiguous final send failure")

    _install_live_boundary_fakes(
        monkeypatch,
        events=events,
        outbound_factory=lambda: (
            _synthetic_trade_outbound(FinalAgreement(2)),
        ),
        send_callback=observe_stage_then_fail,
    )

    result = za_client.run_live_trade(
        _occupied_party(),
        output=target,
        auto_confirm=True,
        transport_factory=za_client.LiveTransport,
    )

    assert pending_seen
    assert result.output_path == target
    assert target.exists()
    assert not list(tmp_path.glob(".crash-proof.pending-*.epa9"))


def test_cancel_during_boundary_log_wins_and_removes_write_ahead_file(
    monkeypatch, tmp_path
) -> None:
    events: list[object] = []
    cancelled = threading.Event()
    log_entered = threading.Event()
    release_log = threading.Event()
    target = tmp_path / "cancelled.epa9"

    def blocking_log(message):
        if "sending local FinalAgreement" in message:
            log_entered.set()
            assert release_log.wait(2)

    _install_live_boundary_fakes(
        monkeypatch,
        events=events,
        outbound_factory=lambda: (
            _synthetic_trade_outbound(FinalAgreement(2)),
        ),
        send_callback=lambda: pytest.fail("FinalAgreement must not be sent"),
    )
    outcome = {}

    def run_trade():
        try:
            outcome["result"] = za_client.run_live_trade(
                _occupied_party(),
                output=target,
                auto_confirm=True,
                cancel_event=cancelled,
                transport_factory=za_client.LiveTransport,
                log=blocking_log,
            )
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run_trade)
    worker.start()
    try:
        assert log_entered.wait(2)
        assert list(tmp_path.glob(".cancelled.pending-*.epa9"))
        cancelled.set()
        release_log.set()
        worker.join(2)
    finally:
        release_log.set()
        worker.join(2)

    assert not worker.is_alive()
    assert isinstance(outcome.get("error"), za_client.OperationCancelled)
    assert "result" not in outcome
    assert events == ["start", "stop"]
    assert not target.exists()
    assert not list(tmp_path.glob(".cancelled.pending-*.epa9"))


def test_remote_cancel_after_final_never_starts_second_auto_trade(
    monkeypatch, tmp_path
) -> None:
    events: list[object] = []
    first_offer = _occupied_party(133).to_bytes(encrypted=True)
    replacement_offer = _occupied_party(150).to_bytes(encrypted=True)
    target = tmp_path / "first-offer-only.epa9"

    class FakeTrade:
        instance = None

        def __init__(self, **_kwargs):
            type(self).instance = self
            self.remote_offer = first_offer
            self.negotiation = SimpleNamespace(
                local_state=TradeState.OFFERED,
                remote_state=TradeState.OFFERED,
                remote_cancel_count=0,
            )
            self.complete = False
            self.unacknowledged = ()
            self.confirmations = 0

        def confirm(self, *, now):
            self.confirmations += 1
            self.negotiation.local_state = TradeState.FINAL_AGREEMENT

    class FakeTransport:
        app_data = b"\x00\x5c\x16\x00\x01" + bytes(0x5C - 5)
        our_mac = b"\x10" * 6
        host_mac = b"\x20" * 6
        our_ip = "169.254.1.2"
        host_ip = "169.254.1.1"
        broadcast = "169.254.1.255"
        ssid = bytes(16)

        def start(self):
            events.append("start")

        def recv(self):
            return []

        def send(self, datagram, destination):
            events.append(("send", datagram, destination))
            trade = FakeTrade.instance
            assert trade is not None
            if len([event for event in events if isinstance(event, tuple)]) == 1:
                trade.negotiation.remote_cancel_count += 1
                trade.negotiation.local_state = TradeState.OFFERED
                trade.negotiation.remote_state = TradeState.OFFERED
                trade.remote_offer = replacement_offer

        def stop(self):
            events.append("stop")

    class FakeClient:
        def __init__(self, _session, trade):
            self.trade = trade
            self.emitted = 0

        def feed_decoded(self, *_args, **_kwargs):
            raise AssertionError("no incoming datagram expected")

        def drain(self, *, now, tick):
            if self.emitted < self.trade.confirmations:
                self.emitted += 1
                return (
                    _synthetic_trade_outbound(FinalAgreement(self.emitted)),
                )
            return ()

    class FakeWriter:
        def __init__(self, *_args):
            pass

        def encode(self, _item):
            return b"wire", "169.254.1.1"

    monkeypatch.setattr(za_client, "ZASession", lambda **_kwargs: object())
    monkeypatch.setattr(za_client, "ReliableTradeEngine", FakeTrade)
    monkeypatch.setattr(za_client, "ZATradeClient", FakeClient)
    monkeypatch.setattr(za_client, "PiaCrypto", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(za_client, "_LivePacketWriter", FakeWriter)

    result = za_client.run_live_trade(
        _occupied_party(),
        output=target,
        auto_confirm=True,
        settle_grace=0,
        transport_factory=FakeTransport,
    )

    assert result.completed is False
    assert result.warning is not None and "remote peer cancelled" in result.warning
    assert PA9(result.received_party_pa9, encrypted=True).species_internal == 133
    assert PA9(target.read_bytes(), encrypted=True).species_internal == 133
    assert FakeTrade.instance.confirmations == 1
    assert events == ["start", ("send", b"wire", "169.254.1.1"), "stop"]


def test_postcommit_logger_baseexception_cannot_discard_result(
    monkeypatch, tmp_path
) -> None:
    class FatalDiagnostic(BaseException):
        pass

    events: list[object] = []
    log_calls = 0
    target = tmp_path / "preserved-with-fatal-log.epa9"

    def fail_send():
        raise RuntimeError("synthetic ambiguous final send failure")

    def fail_cleanup():
        raise OSError("synthetic cleanup failure")

    def unstable_log(_message):
        nonlocal log_calls
        log_calls += 1
        if log_calls >= 2:
            raise FatalDiagnostic("synthetic fatal diagnostic failure")

    _install_live_boundary_fakes(
        monkeypatch,
        events=events,
        outbound_factory=lambda: (
            _synthetic_trade_outbound(FinalAgreement(2)),
        ),
        send_callback=fail_send,
        stop_callback=fail_cleanup,
    )

    result = za_client.run_live_trade(
        _occupied_party(),
        output=target,
        auto_confirm=True,
        transport_factory=za_client.LiveTransport,
        log=unstable_log,
    )

    assert result.completed is False
    assert result.warning is not None
    assert "RuntimeError" in result.warning
    assert "cleanup failed" in result.warning
    assert target.exists()
    assert log_calls == 3
    assert events == ["start", ("send", b"wire", "169.254.1.1"), "stop"]


def test_committed_persistence_absorbs_baseexceptions_and_returns_bytes(
    monkeypatch, tmp_path
) -> None:
    target = tmp_path / "received.epa9"
    approved = za_client._preflight_trade_output(target, overwrite=False)
    received = _occupied_party(133).to_bytes(encrypted=True)

    monkeypatch.setattr(
        za_client,
        "_atomic_save_received_party",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        za_client,
        "_exclusive_recovery_save",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit(2)),
    )

    saved, warning = za_client._persist_committed_party(received, approved)

    assert saved is None
    assert warning is not None and "could not be written" in warning


def test_live_cleanup_failure_preserves_committed_result_and_warns(
    monkeypatch, tmp_path
) -> None:
    cancelled = threading.Event()
    events: list[object] = []
    target = tmp_path / "preserved-before-cleanup-error.epa9"

    def fail_cleanup():
        raise RuntimeError("synthetic radio cleanup failure")

    _install_live_boundary_fakes(
        monkeypatch,
        events=events,
        outbound_factory=lambda: (
            _synthetic_trade_outbound(FinalAgreement(2)),
        ),
        send_callback=cancelled.set,
        stop_callback=fail_cleanup,
    )

    result = za_client.run_live_trade(
        _occupied_party(),
        output=target,
        auto_confirm=True,
        settle_grace=0,
        cancel_event=cancelled,
        transport_factory=za_client.LiveTransport,
    )

    assert result.completed is False
    assert result.warning is not None
    assert "cleanup failed" in result.warning
    assert "do not retry" in result.warning
    assert PA9(target.read_bytes(), encrypted=True).species_internal == 133
    assert events == ["start", ("send", b"wire", "169.254.1.1"), "stop"]


def test_changed_output_target_is_not_overwritten_and_uses_recovery_file(
    tmp_path,
) -> None:
    target = tmp_path / "received.epa9"
    target.write_bytes(b"approved-original")
    approved = za_client._preflight_trade_output(target, overwrite=True)
    replacement = b"changed-after-approval"
    target.write_bytes(replacement)

    received = _occupied_party(150).to_bytes(encrypted=True)
    saved, warning = za_client._persist_committed_party(received, approved)

    assert target.read_bytes() == replacement
    assert saved is not None and saved != target
    assert saved.parent == tmp_path
    assert PA9(saved.read_bytes(), encrypted=True).species_internal == 150
    assert warning is not None and "preserved" in warning


def test_client_compiles_without_finally_control_flow_warning() -> None:
    # Compile source even when an import could reuse cached bytecode. Python
    # 3.14 diagnoses finally-return; warnings-as-errors must remain usable.
    source = Path(za_client.__file__)
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        compile(source.read_bytes(), str(source), "exec")


@pytest.mark.parametrize("boundary", ["start", "nonfinal-send"])
@pytest.mark.parametrize(
    "primary_type", [OSError, za_client.OperationCancelled, KeyboardInterrupt, SystemExit]
)
@pytest.mark.parametrize("cleanup_type", [None, RuntimeError, KeyboardInterrupt, SystemExit])
def test_precommit_cleanup_exception_precedence(
    monkeypatch, tmp_path, boundary, primary_type, cleanup_type
) -> None:
    """No result exists: preserve the exception, or cleanup's chained override."""
    events = []
    primary = primary_type("synthetic precommit failure")
    cleanup = cleanup_type("synthetic cleanup failure") if cleanup_type else None
    target = tmp_path / "not-committed.epa9"

    def fail_primary(*_args):
        if boundary == "start":
            events.append("start")
        raise primary

    def stop():
        if cleanup is not None:
            raise cleanup

    _install_live_boundary_fakes(
        monkeypatch, events=events,
        outbound_factory=lambda: (_synthetic_trade_outbound(ConfirmTrade(2)),),
        send_callback=fail_primary, stop_callback=stop,
    )
    if boundary == "start":
        monkeypatch.setattr(za_client.LiveTransport, "start", fail_primary)
    monkeypatch.setattr(za_client.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        za_client.JoinObservation, "emit", lambda *_args: events.append("observation")
    )

    expected = cleanup if cleanup is not None else primary
    with pytest.raises(type(expected)) as caught:
        za_client.run_live_trade(
            _occupied_party(), output=target, auto_confirm=True,
            transport_factory=za_client.LiveTransport, log=lambda _message: None,
        )

    assert caught.value is expected
    if cleanup is not None:
        assert cleanup.__context__ is primary
    sends = [] if boundary == "start" else [("send", b"wire", "169.254.1.1")]
    assert events == ["start", *sends, "stop", "observation"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "terminal", ["settled", "send-error", "interrupt", "exit", "retry-timeout", "stop", "timeout"]
)
@pytest.mark.parametrize("cleanup_type", [None, OSError, KeyboardInterrupt, SystemExit])
def test_postcommit_cleanup_outcome_matrix(
    monkeypatch, tmp_path, terminal, cleanup_type
) -> None:
    """Synthetic network + real durable I/O; never a physical exchange proof."""
    events = []
    logs = []
    cancelled = threading.Event()
    target = tmp_path / "received.epa9"
    expected_bytes = _occupied_party(133).to_bytes(encrypted=True)
    clock = [0.0]
    persist_calls = []
    real_persist = za_client._persist_committed_party

    def persist(*args):
        persist_calls.append(args)
        return real_persist(*args)

    def send():
        pending = list(tmp_path.glob(".received.pending-*.epa9"))
        assert len(pending) == 1 and pending[0].read_bytes() == expected_bytes
        assert not target.exists()
        if terminal == "send-error":
            raise OSError("synthetic ambiguous send")
        if terminal == "interrupt":
            raise KeyboardInterrupt("synthetic ambiguous send")
        if terminal == "exit":
            raise SystemExit("synthetic ambiguous send")
        if terminal == "stop":
            cancelled.set()
        if terminal == "timeout":
            clock[0] = 1.0

    def stop():
        # Persistence precedes cleanup, including an ambiguous send exception.
        assert target.read_bytes() == expected_bytes
        assert not list(tmp_path.glob(".received.pending-*.epa9"))
        if cleanup_type is not None:
            raise cleanup_type("synthetic cleanup failure")

    _install_live_boundary_fakes(
        monkeypatch, events=events,
        outbound_factory=lambda: (_synthetic_trade_outbound(FinalAgreement(2)),),
        send_callback=send, stop_callback=stop, trade_complete=True,
        trade_unacknowledged=(INITIAL_RELIABLE_SEQUENCE,)
        if terminal in ("retry-timeout", "timeout") else (),
        drain_after_emit_error=ReliableTimeout("synthetic retry exhausted")
        if terminal == "retry-timeout" else None,
    )
    monkeypatch.setattr(za_client, "_persist_committed_party", persist)
    monkeypatch.setattr(za_client.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        za_client.time, "sleep", lambda _seconds: clock.__setitem__(0, clock[0] + 0.01)
    )
    monkeypatch.setattr(
        za_client.JoinObservation, "emit", lambda *_args: events.append("observation")
    )
    result = za_client.run_live_trade(
        _occupied_party(), output=target, auto_confirm=True, timeout=0.5,
        settle_grace=0, cancel_event=cancelled,
        transport_factory=za_client.LiveTransport, log=logs.append,
    )

    assert result.completed is (terminal == "settled" and cleanup_type is None)
    assert result.received_party_pa9 == expected_bytes
    assert result.output_path == target and target.read_bytes() == expected_bytes
    assert result.native_save_sync_complete is False
    assert result.elapsed == (1.01 if terminal == "timeout" else 0.02 if terminal == "retry-timeout" else 0.01)
    assert len(persist_calls) == 1
    assert events == ["start", ("send", b"wire", "169.254.1.1"), "stop", "observation"]
    warnings_by_terminal = {
        "send-error": "OSError occurred", "interrupt": "KeyboardInterrupt occurred",
        "exit": "SystemExit occurred", "retry-timeout": "ReliableTimeout occurred",
        "stop": "stop was requested", "timeout": "acknowledgement or settle grace",
    }
    if terminal != "settled":
        assert warnings_by_terminal[terminal] in result.warning
    if cleanup_type is not None:
        assert f"cleanup failed after FinalAgreement ({cleanup_type.__name__})" in result.warning
        assert "do not retry the trade" in result.warning
        assert result.warning.count("cleanup failed") == 1
        assert sum("cleanup failed" in message for message in logs) == 1
    elif terminal == "settled":
        assert result.warning is None


@pytest.mark.parametrize("cleanup_type", [None, OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("removal_fails", [False, True])
def test_precommit_staged_cleanup_always_runs_after_stop(
    monkeypatch, tmp_path, cleanup_type, removal_fails
) -> None:
    events = []
    cancelled = threading.Event()
    target = tmp_path / "cancelled.epa9"
    remove_stage = za_client._remove_staged_party
    cleanup = cleanup_type("synthetic cleanup failure") if cleanup_type else None

    def log(message):
        if "sending local FinalAgreement" in message:
            assert len(list(tmp_path.glob(".cancelled.pending-*.epa9"))) == 1
            cancelled.set()

    def stop():
        if cleanup is not None:
            raise cleanup

    def remove(path):
        events.append("remove-stage")
        if removal_fails:
            raise SystemExit("synthetic failed stage removal")
        remove_stage(path)

    _install_live_boundary_fakes(
        monkeypatch, events=events,
        outbound_factory=lambda: (_synthetic_trade_outbound(FinalAgreement(2)),),
        send_callback=lambda: pytest.fail("precommit cancellation must prevent send"),
        stop_callback=stop,
    )
    monkeypatch.setattr(za_client.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(za_client, "_remove_staged_party", remove)
    monkeypatch.setattr(
        za_client.JoinObservation, "emit", lambda *_args: events.append("observation")
    )
    with pytest.raises(cleanup_type or za_client.OperationCancelled) as caught:
        za_client.run_live_trade(
            _occupied_party(), output=target, auto_confirm=True, cancel_event=cancelled,
            transport_factory=za_client.LiveTransport, log=log,
        )
    if cleanup is not None:
        assert caught.value is cleanup
        assert isinstance(cleanup.__context__, za_client.OperationCancelled)
    assert not target.exists()
    assert len(list(tmp_path.glob(".cancelled.pending-*.epa9"))) == int(removal_fails)
    assert events == ["start", "stop", "observation", "remove-stage"]


@pytest.mark.parametrize("storage", ["recovery-file", "write-ahead", "memory-only"])
def test_cleanup_failure_preserves_fallback_receipt(monkeypatch, tmp_path, storage) -> None:
    events = []
    expected_bytes = _occupied_party(133).to_bytes(encrypted=True)
    target = tmp_path / "received.epa9"

    def publication_failure(*_args, **_kwargs):
        raise OSError("synthetic publication failure")

    def recovery_failure(*_args, **_kwargs):
        raise KeyboardInterrupt("synthetic recovery failure")

    def cleanup_failure():
        raise SystemExit("synthetic cleanup failure")

    _install_live_boundary_fakes(
        monkeypatch, events=events,
        outbound_factory=lambda: (_synthetic_trade_outbound(FinalAgreement(2)),),
        send_callback=lambda: None, stop_callback=cleanup_failure, trade_complete=True,
    )
    monkeypatch.setattr(za_client.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(za_client, "_atomic_save_received_party", publication_failure)
    if storage == "write-ahead":
        monkeypatch.setattr(za_client, "_exclusive_recovery_save", recovery_failure)
    result = za_client.run_live_trade(
        _occupied_party(), output=None if storage == "memory-only" else target,
        auto_confirm=True, settle_grace=0, transport_factory=za_client.LiveTransport,
        log=lambda _message: None,
    )
    assert not result.completed and result.received_party_pa9 == expected_bytes
    assert "cleanup failed" in result.warning and "do not retry" in result.warning
    assert not target.exists()
    if storage == "memory-only":
        assert result.output_path is None and list(tmp_path.iterdir()) == []
    else:
        assert result.output_path != target
        assert result.output_path.read_bytes() == expected_bytes
        assert list(tmp_path.iterdir()) == [result.output_path]
        if storage == "write-ahead":
            assert "fsynced write-ahead PA9 was preserved" in result.warning
        else:
            assert "approved output could not be replaced" in result.warning
    assert events == ["start", ("send", b"wire", "169.254.1.1"), "stop"]


@pytest.mark.parametrize("recorded_receipt", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_cleanup_override_requires_recorded_result_not_only_persisted_bytes(
    monkeypatch, tmp_path, recorded_receipt, cleanup_fails
) -> None:
    """Pin the old finally precedence even for a pending result-building error."""
    events = []
    target = tmp_path / "received.epa9"
    result_type = za_client.LiveTradeResult
    receipts = []
    report_error = RuntimeError("synthetic result assembly failure")
    cleanup_error = OSError("synthetic cleanup failure")

    def result_factory(*args, **kwargs):
        if not recorded_receipt or receipts:
            raise report_error
        receipt = result_type(*args, **kwargs)
        receipts.append(receipt)
        return receipt

    def interrupt_result_return(frame, event, _arg):
        # Deterministic stand-in for an asynchronous exception after the first
        # receipt is recorded. The inner recovery then fails to build another.
        if (recorded_receipt and event == "return"
                and frame.f_code.co_name == "committed_result"
                and frame.f_globals is vars(za_client)):
            raise KeyboardInterrupt("synthetic interrupt after result recorded")
        return interrupt_result_return

    def stop():
        assert target.read_bytes() == _occupied_party(133).to_bytes(encrypted=True)
        if cleanup_fails:
            raise cleanup_error

    _install_live_boundary_fakes(
        monkeypatch, events=events,
        outbound_factory=lambda: (_synthetic_trade_outbound(FinalAgreement(2)),),
        send_callback=lambda: None, stop_callback=stop, trade_complete=True,
    )
    monkeypatch.setattr(za_client, "LiveTradeResult", result_factory)
    monkeypatch.setattr(za_client.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        za_client.JoinObservation, "emit", lambda *_args: events.append("observation")
    )
    saved_trace = sys.gettrace()
    try:
        if recorded_receipt:
            sys.settrace(interrupt_result_return)
        if recorded_receipt and cleanup_fails:
            result = za_client.run_live_trade(
                _occupied_party(), output=target, auto_confirm=True, settle_grace=0,
                transport_factory=za_client.LiveTransport, log=lambda _message: None,
            )
            assert not result.completed and "do not retry" in result.warning
            assert result.received_party_pa9 == receipts[0].received_party_pa9
            assert result.output_path == receipts[0].output_path == target
            assert result.elapsed == receipts[0].elapsed
        else:
            expected = cleanup_error if cleanup_fails else report_error
            with pytest.raises(type(expected)) as caught:
                za_client.run_live_trade(
                    _occupied_party(), output=target, auto_confirm=True, settle_grace=0,
                    transport_factory=za_client.LiveTransport, log=lambda _message: None,
                )
            assert caught.value is expected
            if cleanup_fails:
                assert cleanup_error.__context__ is report_error
    finally:
        sys.settrace(saved_trace)
    assert len(receipts) == int(recorded_receipt)
    assert events == ["start", ("send", b"wire", "169.254.1.1"), "stop", "observation"]


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_terminal_observation_failure_is_not_silently_swallowed(
    monkeypatch, tmp_path, cleanup_fails
) -> None:
    events = []
    observation_error = RuntimeError("synthetic terminal observation failure")

    def stop():
        if cleanup_fails:
            raise OSError("synthetic cleanup failure")

    def emit(*_args):
        events.append("observation")
        raise observation_error

    _install_live_boundary_fakes(
        monkeypatch, events=events,
        outbound_factory=lambda: (_synthetic_trade_outbound(FinalAgreement(2)),),
        send_callback=lambda: None, stop_callback=stop, trade_complete=True,
    )
    monkeypatch.setattr(za_client.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(za_client.JoinObservation, "emit", emit)
    target = tmp_path / "received.epa9"
    with pytest.raises(RuntimeError) as caught:
        za_client.run_live_trade(
            _occupied_party(), output=target, auto_confirm=True, settle_grace=0,
            transport_factory=za_client.LiveTransport, log=lambda _message: None,
        )
    assert caught.value is observation_error
    assert target.read_bytes() == _occupied_party(133).to_bytes(encrypted=True)
    assert events == ["start", ("send", b"wire", "169.254.1.1"), "stop", "observation"]


@pytest.mark.parametrize("recovery_fails", [False, True])
def test_final_save_collision_preserves_competitor_and_durable_receipt(
    monkeypatch, tmp_path, recovery_fails,
):
    """Actual persistence around synthetic FinalAgreement; no real transport."""
    events = []
    target = tmp_path / "collision.epa9"
    competitor = _occupied_party(25).to_bytes(encrypted=True)
    received = _occupied_party(133).to_bytes(encrypted=True)
    synced_inodes = set()
    real_fsync = za_client.os.fsync

    def track_fsync(fd):
        real_fsync(fd)
        synced_inodes.add(za_client.os.fstat(fd).st_ino)

    def collide_after_final():
        pending = list(tmp_path.glob(".collision.pending-*.epa9"))
        assert len(pending) == 1
        assert pending[0].stat().st_ino in synced_inodes
        assert pending[0].read_bytes() == received
        with target.open("xb") as stream:
            stream.write(competitor)
        raise RuntimeError("synthetic final-send outcome is indeterminate")

    _install_live_boundary_fakes(
        monkeypatch, events=events,
        outbound_factory=lambda: (_synthetic_trade_outbound(FinalAgreement(2)),),
        send_callback=collide_after_final,
    )
    monkeypatch.setattr(za_client.os, "fsync", track_fsync)
    if recovery_fails:
        def reject_recovery(*_args, **_kwargs):
            raise OSError("synthetic recovery failure; retain write-ahead")
        monkeypatch.setattr(za_client, "_exclusive_recovery_save", reject_recovery)
    result = za_client.run_live_trade(
        _occupied_party(), output=target, auto_confirm=True,
        transport_factory=za_client.LiveTransport,
    )
    assert not result.completed and result.warning
    assert result.received_party_pa9 == received
    assert target.read_bytes() == competitor
    assert result.output_path != target and result.output_path.read_bytes() == received
    assert result.output_path.stat().st_ino in synced_inodes
    assert str(result.output_path) in result.warning
    assert len(list(tmp_path.glob(".collision.pending-*.epa9"))) == int(recovery_fails)
    assert len(list(tmp_path.iterdir())) == 2
    assert events == ["start", ("send", b"wire", "169.254.1.1"), "stop"]


@pytest.mark.parametrize("fail_sync", range(1, 7))
@pytest.mark.parametrize("fail_stop", [False, True])
def test_each_durability_boundary_keeps_a_receipt_or_prevents_final_send(
    monkeypatch, tmp_path, fail_sync, fail_stop,
):
    events = []
    target = tmp_path / "sync-fault.epa9"
    received = _occupied_party(133).to_bytes(encrypted=True)
    real_fsync = za_client.os.fsync
    syncs = []
    def sync(fd):
        syncs.append(fd)
        if len(syncs) == fail_sync:
            raise OSError("synthetic disk durability failure")
        real_fsync(fd)
    def uncertain_send():
        raise RuntimeError("synthetic uncertain Final send")
    def stop():
        if fail_stop:
            raise RuntimeError("synthetic cleanup failure")
    _install_live_boundary_fakes(
        monkeypatch, events=events,
        outbound_factory=lambda: (_synthetic_trade_outbound(FinalAgreement(2)),),
        send_callback=uncertain_send, stop_callback=stop,
    )
    monkeypatch.setattr(za_client.os, "fsync", sync)
    if fail_sync <= 2:
        with pytest.raises((OSError, RuntimeError)):
            za_client.run_live_trade(_occupied_party(), output=target,
                auto_confirm=True, transport_factory=za_client.LiveTransport)
        assert events == ["start", "stop"]
        assert not target.exists() and not list(tmp_path.glob("*.pending-*"))
    else:
        result = za_client.run_live_trade(_occupied_party(), output=target,
            auto_confirm=True, transport_factory=za_client.LiveTransport)
        assert not result.completed and result.warning
        assert result.received_party_pa9 == received
        assert result.output_path is not None
        assert result.output_path.read_bytes() == received
        assert not list(tmp_path.glob(".sync-fault.epa9.*.tmp"))
        assert events == ["start", ("send", b"wire", "169.254.1.1"), "stop"]
        if fail_stop:
            assert "cleanup failed" in result.warning
