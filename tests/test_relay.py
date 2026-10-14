from __future__ import annotations

from collections import deque
import socket
import threading
import time

import pytest

from zasim.control import OperationCancelled
from zasim.profile import ZA_PROFILE
from zasim.relay import (
    DEFAULT_RELAY_PORT,
    RelayCleanupError,
    RelayDisconnected,
    RelayEndpoint,
    RelayError,
    RelayProtocolError,
    RelayStartRequest,
    RelayTransport,
    RELAY_PROTOCOL_VERSION,
    ZARelayServer,
    _JsonChannel,
    _parse_start,
    parse_relay_endpoint,
    serve_za_relay,
)


TEST_LINK_CODE = "12345678"


def _link_application_data(code: str = TEST_LINK_CODE) -> bytes:
    return (
        b"\x00\x5c\x16\x00\x06"
        + bytes(0x5C - 5)
        + code.encode("ascii")
        + bytes(8)
        + (8).to_bytes(4, "little")
    )


def _relay_transport(*args, **kwargs) -> RelayTransport:
    kwargs.setdefault("link_code", TEST_LINK_CODE)
    return RelayTransport(*args, **kwargs)


def test_local_za_relay_uses_empirical_channel_hint(monkeypatch) -> None:
    from zasim.ldn import transport as base_transport

    captured = {}

    class FakeTransport:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(base_transport, "LiveTransport", FakeTransport)
    server = ZARelayServer(
        keys_path=None,
        transport_factory=lambda _request: None,  # type: ignore[arg-type]
        log=lambda _message: None,
    )

    result = server._local_transport(RelayStartRequest(
        nickname="EMU",
        communication_id=ZA_PROFILE.title_id_int,
        scene_id=1,
        expected_max_participants=2,
        link_code=TEST_LINK_CODE,
    ))

    assert isinstance(result, FakeTransport)
    assert captured["preferred_scan_channel"] is None
    app_data_matcher = captured["application_data_matcher"]
    assert app_data_matcher(_link_application_data()) is True
    assert app_data_matcher(_link_application_data("87654321")) is False


def test_parse_relay_endpoint_and_loopback_server_policy() -> None:
    four_byte_character = chr(0x1F600)

    assert parse_relay_endpoint("localhost") == RelayEndpoint(
        "localhost", DEFAULT_RELAY_PORT
    )
    assert parse_relay_endpoint("127.0.0.1:25000") == RelayEndpoint(
        "127.0.0.1", 25000
    )
    with pytest.raises(ValueError, match="IPv6"):
        parse_relay_endpoint("[::1]:24872")
    with pytest.raises(ValueError, match="1 through"):
        parse_relay_endpoint("127.0.0.1:0")
    with pytest.raises(ValueError, match="loopback"):
        _relay_transport("192.0.2.10:24872")
    with pytest.raises(ValueError, match="loopback"):
        ZARelayServer(
            keys_path=None,
            listen="0.0.0.0:24872",
            transport_factory=lambda _request: None,  # type: ignore[arg-type]
        )
    assert (
        _relay_transport(nickname=four_byte_character * 8).nickname
        == four_byte_character * 8
    )
    with pytest.raises(ValueError, match="32 UTF-8 bytes"):
        _relay_transport(nickname=four_byte_character * 9)
    with pytest.raises(ValueError, match="20 characters"):
        _relay_transport(nickname="A" * 21)


def test_relay_start_parser_requires_exact_ascii_link_code() -> None:
    message = {
        "type": "start",
        "version": RELAY_PROTOCOL_VERSION,
        "profile": ZA_PROFILE.title_id,
        "nickname": "Win-ZA",
        "communication_id": ZA_PROFILE.title_id_int,
        "scene_id": 1,
        "link_code": TEST_LINK_CODE,
        "require_exact_network": True,
        "expected_max_participants": 2,
    }

    assert _parse_start(message).link_code == TEST_LINK_CODE
    for invalid in (None, "1234567", "１２３４５６７８"):
        malformed = dict(message, link_code=invalid)
        with pytest.raises(RelayProtocolError, match="8 ASCII digits"):
            _parse_start(malformed)


def test_relay_start_parser_enforces_utf8_nickname_size() -> None:
    four_byte_character = chr(0x1F600)
    message = {
        "type": "start",
        "version": RELAY_PROTOCOL_VERSION,
        "profile": ZA_PROFILE.title_id,
        "nickname": four_byte_character * 8,
        "communication_id": ZA_PROFILE.title_id_int,
        "scene_id": 1,
        "link_code": TEST_LINK_CODE,
        "require_exact_network": True,
        "expected_max_participants": 2,
    }

    assert _parse_start(message).nickname == four_byte_character * 8
    with pytest.raises(RelayProtocolError, match="32 UTF-8 bytes"):
        _parse_start(dict(message, nickname=four_byte_character * 9))


class _FakeLinuxTransport:
    def __init__(self) -> None:
        self.ssid = bytes(range(16))
        self.our_ip = "169.254.50.2"
        self.host_ip = "169.254.50.1"
        self.our_mac = bytes.fromhex("001122334455")
        self.host_mac = bytes.fromhex("aabbccddeeff")
        self.broadcast = "169.254.50.255"
        self.app_data = _link_application_data()
        self.network_app_version = None
        self.LOCAL_COMMUNICATION_ID = ZA_PROFILE.title_id_int
        self.SCENE_ID = 1
        self.APPLICATION_VERSION = 1
        self.started = 0
        self.stopped = 0
        self.incoming = deque(
            [(b"first", self.host_ip), (b"second", self.host_ip)]
        )
        self.sent: list[tuple[bytes, str]] = []
        self.lock = threading.Lock()

    def start(self):
        self.started += 1
        return self

    def recv(self):
        with self.lock:
            packets = list(self.incoming)
            self.incoming.clear()
            return packets

    def send(self, datagram, dst_ip):
        with self.lock:
            self.sent.append((bytes(datagram), dst_ip))

    def stop(self):
        self.stopped += 1


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("timed out waiting for relay test condition")
        time.sleep(0.005)


def test_loopback_relay_roundtrip_and_cleanup() -> None:
    fake = _FakeLinuxTransport()
    requests = []
    server = ZARelayServer(
        keys_path=None,
        listen="127.0.0.1:0",
        transport_factory=lambda request: requests.append(request) or fake,
        log=lambda _message: None,
    )
    endpoint = server.open()
    outcome = {}

    def run_server() -> None:
        try:
            outcome["result"] = server.serve_once()
        except BaseException as exc:  # surfaced in the main test thread below
            outcome["error"] = exc

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    client = _relay_transport(
        str(endpoint), nickname="Win-ZA", startup_timeout=2, log=lambda _message: None
    )
    try:
        assert client.start() is client
        assert client.ssid == fake.ssid
        assert client.our_ip == fake.our_ip
        assert client.host_ip == fake.host_ip
        assert client.our_mac == fake.our_mac
        assert client.host_mac == fake.host_mac
        assert client.app_data == fake.app_data
        assert client.network_app_version == 1
        assert requests[0].nickname == "Win-ZA"
        assert requests[0].communication_id == ZA_PROFILE.title_id_int
        assert requests[0].scene_id == 1
        assert requests[0].link_code == TEST_LINK_CODE

        # The relay reader must keep draining TCP while an interactive trade
        # confirmation blocks the main thread and therefore does not call recv().
        _wait_until(lambda: len(client._packets) == 2)

        packets = []

        def received_both() -> bool:
            packets.extend(client.recv())
            return len(packets) == 2

        _wait_until(received_both)
        assert packets == [(b"first", fake.host_ip), (b"second", fake.host_ip)]

        client.send(b"outbound", fake.host_ip)
        _wait_until(lambda: bool(fake.sent))
        assert fake.sent == [(b"outbound", fake.host_ip)]
        with pytest.raises(ValueError, match="joined host or broadcast"):
            client.send(b"blocked", "192.0.2.1")
    finally:
        client.stop()
        thread.join(timeout=2)
        server.close()

    assert not thread.is_alive()
    assert "error" not in outcome
    assert fake.started == 1
    assert fake.stopped == 1
    assert outcome["result"]["packets_to_windows"] == 2
    assert outcome["result"]["packets_from_windows"] == 1


def test_relay_stop_waits_for_linux_transport_cleanup() -> None:
    cleanup_entered = threading.Event()
    allow_cleanup = threading.Event()
    cleanup_done = threading.Event()

    class BlockingCleanupTransport(_FakeLinuxTransport):
        def stop(self):
            cleanup_entered.set()
            assert allow_cleanup.wait(2)
            super().stop()
            cleanup_done.set()

    fake = BlockingCleanupTransport()
    fake.incoming.clear()
    server = ZARelayServer(
        keys_path=None,
        listen="127.0.0.1:0",
        transport_factory=lambda _request: fake,
        log=lambda _message: None,
    )
    endpoint = server.open()
    server_outcome = {}

    def run_server() -> None:
        try:
            server_outcome["result"] = server.serve_once()
        except BaseException as exc:
            server_outcome["error"] = exc

    server_thread = threading.Thread(target=run_server)
    server_thread.start()
    client = _relay_transport(str(endpoint), startup_timeout=2, log=lambda _message: None)
    client.start()
    stop_thread = threading.Thread(target=client.stop)
    stop_thread.start()
    try:
        assert cleanup_entered.wait(1)
        assert stop_thread.is_alive()
        assert not cleanup_done.is_set()
        allow_cleanup.set()
        stop_thread.join(2)
        server_thread.join(2)
    finally:
        allow_cleanup.set()
        client.stop()
        server.close()

    assert not stop_thread.is_alive()
    assert not server_thread.is_alive()
    assert cleanup_done.is_set()
    assert fake.stopped == 1
    assert "error" not in server_outcome


def test_relay_startup_cancel_waits_for_linux_transport_cleanup() -> None:
    startup_entered = threading.Event()
    cleanup_entered = threading.Event()
    allow_cleanup = threading.Event()
    cleanup_done = threading.Event()

    class CancelAwareTransport(_FakeLinuxTransport):
        def start(self, *, cancel_event=None):
            assert cancel_event is not None
            startup_entered.set()
            assert cancel_event.wait(2)
            return self

        def stop(self):
            cleanup_entered.set()
            assert allow_cleanup.wait(2)
            super().stop()
            cleanup_done.set()

    fake = CancelAwareTransport()
    fake.incoming.clear()
    server = ZARelayServer(
        keys_path=None,
        listen="127.0.0.1:0",
        transport_factory=lambda _request: fake,
        log=lambda _message: None,
    )
    endpoint = server.open()
    server_outcome = {}

    def run_server() -> None:
        try:
            server_outcome["result"] = server.serve_once()
        except BaseException as exc:
            server_outcome["error"] = exc

    server_thread = threading.Thread(target=run_server)
    server_thread.start()
    cancelled = threading.Event()
    client = _relay_transport(str(endpoint), startup_timeout=2, log=lambda _message: None)
    client_outcome = {}

    def start_client() -> None:
        try:
            client.start(cancel_event=cancelled)
        except BaseException as exc:
            client_outcome["error"] = exc

    client_thread = threading.Thread(target=start_client)
    client_thread.start()
    try:
        assert startup_entered.wait(1)
        cancelled.set()
        assert cleanup_entered.wait(1)
        assert client_thread.is_alive()
        assert not cleanup_done.is_set()
        allow_cleanup.set()
        client_thread.join(2)
        server_thread.join(2)
    finally:
        allow_cleanup.set()
        client.stop()
        server.close()

    assert not client_thread.is_alive()
    assert not server_thread.is_alive()
    assert cleanup_done.is_set()
    assert isinstance(client_outcome.get("error"), OperationCancelled)
    assert "error" not in server_outcome


def test_relay_startup_failure_waits_for_linux_transport_cleanup() -> None:
    cleanup_entered = threading.Event()
    allow_cleanup = threading.Event()
    cleanup_done = threading.Event()

    class FailingStartBlockingCleanupTransport(_FakeLinuxTransport):
        def start(self, *, cancel_event=None):
            self.started += 1
            raise RuntimeError("synthetic startup failure")

        def stop(self):
            cleanup_entered.set()
            assert allow_cleanup.wait(2)
            super().stop()
            cleanup_done.set()

    fake = FailingStartBlockingCleanupTransport()
    fake.incoming.clear()
    server = ZARelayServer(
        keys_path=None,
        listen="127.0.0.1:0",
        transport_factory=lambda _request: fake,
        log=lambda _message: None,
    )
    endpoint = server.open()
    server_outcome = {}

    def run_server() -> None:
        try:
            server_outcome["result"] = server.serve_once()
        except BaseException as exc:
            server_outcome["error"] = exc

    server_thread = threading.Thread(target=run_server)
    server_thread.start()
    client = _relay_transport(
        str(endpoint), startup_timeout=2, log=lambda _message: None
    )
    client_outcome = {}

    def start_client() -> None:
        try:
            client.start()
        except BaseException as exc:
            client_outcome["error"] = exc

    client_thread = threading.Thread(target=start_client)
    client_thread.start()
    try:
        assert cleanup_entered.wait(1)
        assert client_thread.is_alive()
        assert "error" not in client_outcome
        assert not cleanup_done.is_set()
        allow_cleanup.set()
        client_thread.join(2)
        server_thread.join(2)
    finally:
        allow_cleanup.set()
        client.stop()
        server.close()

    assert not client_thread.is_alive()
    assert not server_thread.is_alive()
    assert cleanup_done.is_set()
    assert fake.started == 1
    assert fake.stopped == 1
    assert isinstance(client_outcome.get("error"), RelayError)
    assert "Linux LDN startup failed" in str(client_outcome["error"])
    assert isinstance(server_outcome.get("error"), RelayError)


def test_relay_link_code_validation_failure_waits_for_linux_cleanup() -> None:
    cleanup_entered = threading.Event()
    allow_cleanup = threading.Event()
    cleanup_done = threading.Event()

    class InvalidStartedBlockingCleanupTransport(_FakeLinuxTransport):
        def __init__(self) -> None:
            super().__init__()
            self.app_data = _link_application_data("87654321")

        def stop(self):
            cleanup_entered.set()
            assert allow_cleanup.wait(2)
            super().stop()
            cleanup_done.set()

    fake = InvalidStartedBlockingCleanupTransport()
    fake.incoming.clear()
    server = ZARelayServer(
        keys_path=None,
        listen="127.0.0.1:0",
        transport_factory=lambda _request: fake,
        log=lambda _message: None,
    )
    endpoint = server.open()
    server_outcome = {}

    def run_server() -> None:
        try:
            server_outcome["result"] = server.serve_once()
        except BaseException as exc:
            server_outcome["error"] = exc

    server_thread = threading.Thread(target=run_server)
    server_thread.start()
    client = _relay_transport(str(endpoint), startup_timeout=2, log=lambda _message: None)
    client_outcome = {}

    def start_client() -> None:
        try:
            client.start()
        except BaseException as exc:
            client_outcome["error"] = exc

    client_thread = threading.Thread(target=start_client)
    client_thread.start()
    try:
        assert cleanup_entered.wait(1)
        assert client_thread.is_alive()
        assert "error" not in client_outcome
        assert not cleanup_done.is_set()
        allow_cleanup.set()
        client_thread.join(2)
        server_thread.join(2)
    finally:
        allow_cleanup.set()
        client.stop()
        server.close()

    assert not client_thread.is_alive()
    assert not server_thread.is_alive()
    assert cleanup_done.is_set()
    assert fake.stopped == 1
    error = client_outcome.get("error")
    assert isinstance(error, RelayProtocolError)
    assert "different or malformed Link Code" in str(error)
    assert "error" not in server_outcome


def test_relay_clean_stop_cleanup_failure_is_server_fatal() -> None:
    class BrokenCleanupTransport(_FakeLinuxTransport):
        def stop(self):
            self.stopped += 1
            raise OSError("synthetic radio cleanup failure")

    fake = BrokenCleanupTransport()
    fake.incoming.clear()
    server = ZARelayServer(
        keys_path=None,
        listen="127.0.0.1:0",
        transport_factory=lambda _request: fake,
        log=lambda _message: None,
    )
    endpoint = server.open()
    server_outcome = {}

    def run_server() -> None:
        try:
            server_outcome["result"] = server.serve_once()
        except BaseException as exc:
            server_outcome["error"] = exc

    server_thread = threading.Thread(target=run_server)
    server_thread.start()
    client = _relay_transport(str(endpoint), startup_timeout=2, log=lambda _message: None)
    client.start()
    try:
        with pytest.raises(RelayError, match="without confirming radio cleanup"):
            client.stop()
        server_thread.join(2)
    finally:
        server.close()

    assert not server_thread.is_alive()
    error = server_outcome.get("error")
    assert isinstance(error, RelayCleanupError)
    assert isinstance(error.cleanup_error, OSError)
    assert error.primary_error is None
    assert fake.stopped == 1
    assert server._failed_transport is fake
    with pytest.raises(RelayCleanupError) as repeated:
        server.serve_once()
    assert repeated.value is error


def test_relay_protocol_error_waits_for_linux_transport_cleanup() -> None:
    cleanup_entered = threading.Event()
    allow_cleanup = threading.Event()
    cleanup_done = threading.Event()

    class BlockingCleanupTransport(_FakeLinuxTransport):
        def stop(self):
            cleanup_entered.set()
            assert allow_cleanup.wait(2)
            super().stop()
            cleanup_done.set()

    fake = BlockingCleanupTransport()
    fake.incoming.clear()
    server = ZARelayServer(
        keys_path=None,
        listen="127.0.0.1:0",
        transport_factory=lambda _request: fake,
        log=lambda _message: None,
    )
    endpoint = server.open()
    server_outcome = {}

    def run_server() -> None:
        try:
            server_outcome["result"] = server.serve_once()
        except BaseException as exc:
            server_outcome["error"] = exc

    server_thread = threading.Thread(target=run_server)
    server_thread.start()
    client = _relay_transport(str(endpoint), startup_timeout=2, log=lambda _message: None)
    client.start()
    try:
        channel = client._channel
        assert channel is not None
        channel.send({"type": "unexpected"})
        assert cleanup_entered.wait(1)
        assert server_thread.is_alive()
        assert not cleanup_done.is_set()
        assert not client._reader_done.is_set()

        allow_cleanup.set()
        server_thread.join(2)
        _wait_until(client._reader_done.is_set)
        with pytest.raises(RelayError, match="unexpected client relay message type"):
            client.recv()
    finally:
        allow_cleanup.set()
        try:
            client.stop()
        except RelayError:
            pass
        server.close()

    assert not server_thread.is_alive()
    assert cleanup_done.is_set()
    assert fake.stopped == 1
    assert isinstance(server_outcome.get("error"), RelayProtocolError)


def test_protocol_error_is_not_masked_or_retried_when_cleanup_fails() -> None:
    class BrokenCleanupTransport(_FakeLinuxTransport):
        def stop(self):
            self.stopped += 1
            raise OSError("synthetic radio cleanup failure")

    fake = BrokenCleanupTransport()
    fake.incoming.clear()
    messages = []
    server = ZARelayServer(
        keys_path=None,
        listen="127.0.0.1:0",
        transport_factory=lambda _request: fake,
        log=messages.append,
    )
    endpoint = server.open()
    server_outcome = {}

    def run_server() -> None:
        try:
            server_outcome["result"] = server.serve_once()
        except BaseException as exc:
            server_outcome["error"] = exc

    server_thread = threading.Thread(target=run_server)
    server_thread.start()
    client = _relay_transport(str(endpoint), startup_timeout=2, log=lambda _message: None)
    client.start()
    try:
        channel = client._channel
        assert channel is not None
        channel.send({"type": "unexpected"})
        server_thread.join(2)
        _wait_until(client._reader_done.is_set)
        with pytest.raises(RelayDisconnected, match="disconnected"):
            client.recv()
    finally:
        try:
            client.stop()
        except RelayError:
            pass
        server.close()

    assert not server_thread.is_alive()
    error = server_outcome.get("error")
    assert isinstance(error, RelayCleanupError)
    assert isinstance(error.cleanup_error, OSError)
    assert isinstance(error.primary_error, RelayProtocolError)
    assert "unexpected client relay message type" in str(error.primary_error)
    assert fake.stopped == 1
    assert any("original session also failed" in message for message in messages)


def test_relay_service_fail_stops_after_start_and_cleanup_failure(
    monkeypatch,
) -> None:
    class BrokenFirstTransport(_FakeLinuxTransport):
        def start(self, *, cancel_event=None):
            self.started += 1
            raise RuntimeError("synthetic startup failure")

        def stop(self):
            self.stopped += 1
            raise OSError("synthetic radio cleanup failure")

    first = BrokenFirstTransport()
    second = _FakeLinuxTransport()
    factory_calls = []
    servers = []

    def transport_factory(_request):
        transport = first if not factory_calls else second
        factory_calls.append(transport)
        return transport

    real_server = ZARelayServer

    class InjectedRelayServer(real_server):
        def __init__(self, **kwargs):
            kwargs["transport_factory"] = transport_factory
            super().__init__(**kwargs)
            servers.append(self)

    monkeypatch.setattr("zasim.relay.ZARelayServer", InjectedRelayServer)
    outcome = {}

    def run_service() -> None:
        try:
            serve_za_relay(
                keys_path="unused-prod.keys",
                listen="127.0.0.1:0",
                log=lambda _message: None,
            )
        except BaseException as exc:
            outcome["error"] = exc

    service_thread = threading.Thread(target=run_service, daemon=True)
    service_thread.start()
    _wait_until(lambda: bool(servers) and servers[0].bound_endpoint is not None)
    endpoint = servers[0].bound_endpoint
    assert endpoint is not None

    client = _relay_transport(str(endpoint), startup_timeout=2, log=lambda _message: None)
    try:
        with pytest.raises(RelayError, match="did not confirm radio cleanup"):
            client.start()
        service_thread.join(2)
    finally:
        client.stop()
        servers[0].close()

    assert not service_thread.is_alive()
    error = outcome.get("error")
    assert isinstance(error, RelayCleanupError)
    assert isinstance(error.cleanup_error, OSError)
    assert isinstance(error.primary_error, RelayError)
    assert isinstance(error.primary_error.__cause__, RuntimeError)
    assert factory_calls == [first]
    assert first.started == 1
    assert first.stopped == 1
    assert second.started == 0
    assert servers[0]._failed_transport is first


def test_json_channel_delivers_complete_message_before_reporting_peer_eof() -> None:
    receiver, sender = socket.socketpair()
    channel = _JsonChannel(receiver)
    try:
        sender.sendall(b'{"type":"error","message":"finished"}\n')
        sender.shutdown(socket.SHUT_WR)

        assert channel.receive_available() == (
            {"type": "error", "message": "finished"},
        )
        with pytest.raises(RelayDisconnected, match="disconnected"):
            channel.receive_available()
    finally:
        channel.close()
        sender.close()


def test_json_channel_normalizes_parser_limits_to_protocol_error() -> None:
    receiver, sender = socket.socketpair()
    channel = _JsonChannel(receiver)
    raw = b'{"type":' + (b"[" * 1200) + b"0" + (b"]" * 1200) + b"}"
    try:
        with pytest.raises(RelayProtocolError, match="malformed JSON"):
            channel._feed(raw + b"\n")
    finally:
        channel.close()
        sender.close()


def test_json_channel_nesting_limit_ignores_brackets_inside_strings() -> None:
    receiver, sender = socket.socketpair()
    channel = _JsonChannel(receiver)
    try:
        channel._feed(b'{"type":"[[[[{{{{","message":"\\\"["}\n')
        assert channel.pending.popleft()["type"] == "[[[[{{{{"
    finally:
        channel.close()
        sender.close()


def test_json_channel_normalizes_json_value_error(monkeypatch) -> None:
    receiver, sender = socket.socketpair()
    channel = _JsonChannel(receiver)

    def parser_limit(_raw):
        raise ValueError("integer conversion limit")

    monkeypatch.setattr("zasim.relay.json.loads", parser_limit)
    try:
        with pytest.raises(RelayProtocolError, match="malformed JSON"):
            channel._feed(b'{"type":1}\n')
    finally:
        channel.close()
        sender.close()


def test_relay_reader_records_unexpected_failure_instead_of_dying_silently() -> None:
    class BrokenChannel:
        def receive_available(self):
            raise LookupError("synthetic reader failure")

    client = _relay_transport(log=lambda _message: None)
    client._prepare_reader()
    client._read_relay(BrokenChannel())  # type: ignore[arg-type]
    assert isinstance(client._reader_error, RelayProtocolError)
    assert client._reader_done.is_set()


def test_relay_client_reports_connection_failure_without_keys_or_data_leak() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    endpoint = RelayEndpoint("127.0.0.1", listener.getsockname()[1])
    listener.close()

    with pytest.raises(OSError):
        _relay_transport(str(endpoint), startup_timeout=0.1).start()


def test_relay_start_requires_explicit_link_code_before_connecting() -> None:
    with pytest.raises(ValueError, match="8-digit Link Code"):
        RelayTransport(log=lambda _message: None).start()


def test_relay_start_can_be_cancelled_while_waiting_for_linux() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    endpoint = RelayEndpoint("127.0.0.1", listener.getsockname()[1])
    accepted = threading.Event()
    release_server = threading.Event()

    def stall_server() -> None:
        connection, _peer = listener.accept()
        try:
            request = bytearray()
            while b"\n" not in request:
                request.extend(connection.recv(4096))
            accepted.set()
            release_server.wait(2)
        finally:
            connection.close()

    server_thread = threading.Thread(target=stall_server)
    server_thread.start()
    cancelled = threading.Event()
    outcome = {}

    def start_client() -> None:
        try:
            _relay_transport(str(endpoint), startup_timeout=5).start(
                cancel_event=cancelled
            )
        except BaseException as exc:
            outcome["error"] = exc

    client_thread = threading.Thread(target=start_client)
    client_thread.start()
    try:
        assert accepted.wait(1)
        cancelled.set()
        client_thread.join(2)
        assert not client_thread.is_alive()
        error = outcome.get("error")
        assert isinstance(error, RelayError)
        assert not isinstance(error, OperationCancelled)
        assert "did not confirm radio cleanup" in str(error)
    finally:
        release_server.set()
        server_thread.join(2)
        listener.close()


def test_cancelled_start_remote_error_confirms_cleanup_without_detail_leak() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    endpoint = RelayEndpoint("127.0.0.1", listener.getsockname()[1])
    start_received = threading.Event()
    cancelled = threading.Event()
    secret = "REMOTE-PRIVATE-DETAIL"

    def server() -> None:
        connection, _peer = listener.accept()
        channel = _JsonChannel(connection)
        try:
            assert channel.receive_one(2)["type"] == "start"
            start_received.set()
            assert channel.receive_one(2)["type"] == "stop"
            channel.send({"type": "error", "message": secret})
        finally:
            channel.close()

    server_thread = threading.Thread(target=server)
    server_thread.start()
    messages = []
    client = _relay_transport(
        str(endpoint), startup_timeout=2, log=messages.append
    )
    outcome = {}

    def start_client() -> None:
        try:
            client.start(cancel_event=cancelled)
        except BaseException as exc:
            outcome["error"] = exc

    client_thread = threading.Thread(target=start_client)
    client_thread.start()
    try:
        assert start_received.wait(1)
        cancelled.set()
        client_thread.join(2)
        server_thread.join(2)
    finally:
        client.stop()
        listener.close()

    assert not client_thread.is_alive()
    assert not server_thread.is_alive()
    error = outcome.get("error")
    assert isinstance(error, OperationCancelled)
    assert secret not in str(error)
    assert all(secret not in message for message in messages)


def test_cancel_at_started_boundary_still_waits_for_cleanup() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    endpoint = RelayEndpoint("127.0.0.1", listener.getsockname()[1])
    start_received = threading.Event()
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    cancelled = threading.Event()

    def server() -> None:
        connection, _peer = listener.accept()
        channel = _JsonChannel(connection)
        try:
            assert channel.receive_one(2)["type"] == "start"
            start_received.set()
            assert cancelled.wait(2)
            channel.send(
                {
                    "type": "started",
                    "version": RELAY_PROTOCOL_VERSION,
                    "profile": ZA_PROFILE.title_id,
                    "ssid": bytes(16).hex(),
                    "our_ip": "169.254.1.2",
                    "host_ip": "169.254.1.1",
                    "our_mac": bytes(6).hex(),
                    "host_mac": bytes(6).hex(),
                    "broadcast": "169.254.1.255",
                    "application_data": _link_application_data().hex(),
                    "application_version": 1,
                    "communication_id": ZA_PROFILE.title_id_int,
                    "scene_id": 1,
                }
            )
            assert channel.receive_one(2)["type"] == "stop"
            cleanup_started.set()
            assert allow_cleanup.wait(2)
            channel.send({"type": "stopped"})
        finally:
            channel.close()

    server_thread = threading.Thread(target=server)
    server_thread.start()
    outcome = {}

    def client() -> None:
        try:
            _relay_transport(str(endpoint), startup_timeout=2).start(
                cancel_event=cancelled
            )
        except BaseException as exc:
            outcome["error"] = exc

    client_thread = threading.Thread(target=client)
    client_thread.start()
    try:
        assert start_received.wait(1)
        cancelled.set()
        assert cleanup_started.wait(1)
        assert client_thread.is_alive()
        allow_cleanup.set()
        client_thread.join(2)
        assert isinstance(outcome.get("error"), OperationCancelled)
    finally:
        allow_cleanup.set()
        client_thread.join(2)
        server_thread.join(2)
        listener.close()


def test_relay_service_accepts_sequential_sessions_by_default(monkeypatch) -> None:
    instances = []

    class FakeServer:
        def __init__(self, **_kwargs):
            self.calls = 0
            self.closed = False
            instances.append(self)

        def open(self):
            return RelayEndpoint("127.0.0.1", 24872)

        def serve_once(self):
            self.calls += 1
            if self.calls == 3:
                raise KeyboardInterrupt
            return {
                "listen": "127.0.0.1:24872",
                "packets_to_windows": 0,
                "packets_from_windows": 0,
            }

        def close(self):
            self.closed = True

    monkeypatch.setattr("zasim.relay.ZARelayServer", FakeServer)

    with pytest.raises(KeyboardInterrupt):
        serve_za_relay(keys_path="prod.keys", log=lambda _message: None)

    assert instances[0].calls == 3
    assert instances[0].closed is True
