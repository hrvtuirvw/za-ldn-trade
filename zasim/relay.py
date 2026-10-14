"""Loopback-only relay for using the Z-A client from Windows.

Windows runs the Pia, Reliable, PA9, and trade state-machine layers.  A small
Linux/WSL2 process owns the Wi-Fi PHY and forwards only Pia UDP datagrams and
the public LDN session metadata required by those layers.  ``prod.keys`` stays
on the Linux side and is never part of this protocol.

The server intentionally binds only to an IPv4 loopback address.  A Windows
machine using a separate Linux host should reach it through an SSH local port
forward instead of exposing unencrypted Pokemon data on the LAN.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import ipaddress
import json
import math
import os
import select
import socket
import threading
import time
from typing import Callable, Protocol, runtime_checkable

from zasim.ldn.ldn_name import validate_ldn_nickname

from .transport_setup import make_local_transport
from .control import (
    CancellationToken,
    OperationCancelled,
    raise_if_cancelled,
    start_transport,
    validate_cancel_event,
)
from .link_code import advertised_link_code_matches, validate_link_code
from .profile import (
    ZA_BOX_TRADE_MAX_PARTICIPANTS,
    ZA_PROFILE,
)
from .text import display_safe
from .json_limits import validate_json_depth


RELAY_PROTOCOL_VERSION = 2
DEFAULT_RELAY_HOST = "127.0.0.1"
DEFAULT_RELAY_PORT = 24872
DEFAULT_RELAY_ENDPOINT = f"{DEFAULT_RELAY_HOST}:{DEFAULT_RELAY_PORT}"
MAX_RELAY_LINE = 16 * 1024
MAX_RELAY_JSON_DEPTH = 32
MAX_RELAY_DATAGRAM = ZA_PROFILE.max_packet_size
RELAY_HANDSHAKE_TIMEOUT = 120.0
RELAY_IO_TIMEOUT = 2.0
# Linux cleanup includes cancelling the LDN nursery, removing the virtual
# interface, and restoring the host radio.  The client must not mistake the
# old, pre-cleanup acknowledgement for a completed stop.
RELAY_STOP_TIMEOUT = 10.0
RELAY_CANCEL_NEGOTIATION_TIMEOUT = 0.75
MAX_RELAY_QUEUE_DATAGRAMS = 4096


class RelayError(RuntimeError):
    """Base class for relay connection and protocol failures."""


class RelayProtocolError(RelayError):
    """A relay peer sent malformed or unsafe data."""


class RelayDisconnected(RelayError):
    """The relay connection closed while the live client was active."""


class RelayCleanupError(RelayError):
    """Fatal cleanup failure after which another relay session is unsafe."""

    def __init__(
        self,
        message: str,
        *,
        cleanup_error: BaseException,
        primary_error: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.cleanup_error = cleanup_error
        self.primary_error = primary_error


@dataclass(frozen=True, slots=True)
class RelayEndpoint:
    host: str
    port: int

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


def parse_relay_endpoint(
    value: str | RelayEndpoint,
    *,
    allow_zero_port: bool = False,
) -> RelayEndpoint:
    """Parse ``HOST[:PORT]``; unbracketed IPv6 is deliberately unsupported."""

    if isinstance(value, RelayEndpoint):
        endpoint = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("relay endpoint cannot be empty")
        if text.startswith("[") or text.count(":") > 1:
            raise ValueError(
                "relay endpoint must use an IPv4 address or hostname, not IPv6"
            )
        if ":" in text:
            host, port_text = text.rsplit(":", 1)
            if not host or not port_text:
                raise ValueError("relay endpoint must be HOST[:PORT]")
            try:
                port = int(port_text, 10)
            except ValueError as exc:
                raise ValueError("relay port must be a decimal integer") from exc
        else:
            host, port = text, DEFAULT_RELAY_PORT
        endpoint = RelayEndpoint(host, port)
    else:
        raise TypeError("relay endpoint must be a string or RelayEndpoint")

    if not isinstance(endpoint.port, int) or isinstance(endpoint.port, bool):
        raise TypeError("relay port must be an integer")
    minimum = 0 if allow_zero_port else 1
    if not minimum <= endpoint.port <= 65535:
        qualifier = "0 through" if allow_zero_port else "1 through"
        raise ValueError(f"relay port must be from {qualifier} 65535")
    if not endpoint.host or any(character.isspace() for character in endpoint.host):
        raise ValueError("relay host cannot be empty or contain whitespace")
    return endpoint


@runtime_checkable
class DatagramTransport(Protocol):
    """Structural contract shared by local and relayed live transports."""

    ssid: bytes
    our_ip: str
    host_ip: str
    our_mac: bytes
    host_mac: bytes
    broadcast: str
    app_data: bytes
    network_app_version: int
    LOCAL_COMMUNICATION_ID: int
    SCENE_ID: int
    APPLICATION_VERSION: int

    def start(
        self, *, cancel_event: CancellationToken | None = None
    ) -> "DatagramTransport": ...

    def recv(self) -> list[tuple[bytes, str]]: ...

    def send(self, datagram: bytes, dst_ip: str) -> None: ...

    def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RelayStartRequest:
    nickname: str
    communication_id: int
    scene_id: int
    expected_max_participants: int
    link_code: str


def _json_line(message: dict[str, object]) -> bytes:
    try:
        encoded = json.dumps(
            message,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RelayProtocolError("relay message is not JSON serializable") from exc
    if not encoded or len(encoded) > MAX_RELAY_LINE:
        raise RelayProtocolError("relay JSON message exceeds the line-size limit")
    return encoded + b"\n"


def _validate_json_nesting(raw: bytes) -> None:
    """Use the shared scanner with the relay's stricter cap and error API."""
    try:
        validate_json_depth(raw, max_depth=MAX_RELAY_JSON_DEPTH)
    except ValueError:
        raise RelayProtocolError(
            "relay sent malformed JSON (nesting limit exceeded)"
        ) from None


class _JsonChannel:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.buffer = bytearray()
        self.pending: deque[dict[str, object]] = deque()
        self.closed = False
        self.peer_eof = False

    def send(self, message: dict[str, object]) -> None:
        if self.closed or self.peer_eof:
            raise RelayDisconnected("relay connection is closed")
        try:
            self.connection.sendall(_json_line(message))
        except (OSError, TimeoutError) as exc:
            self.closed = True
            raise RelayDisconnected("relay connection closed while sending") from exc

    def _feed(self, data: bytes) -> None:
        self.buffer.extend(data)
        while True:
            newline = self.buffer.find(b"\n")
            if newline < 0:
                break
            if newline > MAX_RELAY_LINE:
                raise RelayProtocolError("relay JSON line exceeds the size limit")
            raw = bytes(self.buffer[:newline])
            del self.buffer[: newline + 1]
            if not raw:
                raise RelayProtocolError("relay sent an empty JSON line")
            _validate_json_nesting(raw)
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                raise RelayProtocolError("relay sent malformed JSON") from exc
            if not isinstance(message, dict):
                raise RelayProtocolError("relay JSON message must be an object")
            self.pending.append(message)
        if len(self.buffer) > MAX_RELAY_LINE:
            raise RelayProtocolError("relay JSON line exceeds the size limit")

    def receive_one(
        self,
        timeout: float,
        cancel_event: CancellationToken | None = None,
    ) -> dict[str, object]:
        validate_cancel_event(cancel_event)
        if self.pending:
            return self.pending.popleft()
        if self.closed or self.peer_eof:
            raise RelayDisconnected("relay connection is closed")
        deadline = time.monotonic() + timeout
        while True:
            raise_if_cancelled(cancel_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RelayError("timed out waiting for the Linux LDN relay")
            poll_timeout = min(remaining, 0.1) if cancel_event is not None else remaining
            self.connection.settimeout(poll_timeout)
            try:
                data = self.connection.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                self.closed = True
                raise RelayDisconnected("relay connection closed during startup") from exc
            if not data:
                self.peer_eof = True
                if self.buffer:
                    raise RelayProtocolError(
                        "relay closed with an incomplete JSON message"
                    )
                raise RelayDisconnected("relay connection closed during startup")
            self._feed(data)
            if self.pending:
                return self.pending.popleft()

    def receive_available(self) -> tuple[dict[str, object], ...]:
        messages: list[dict[str, object]] = []
        while self.pending:
            messages.append(self.pending.popleft())
        if self.closed or self.peer_eof:
            if messages:
                return tuple(messages)
            raise RelayDisconnected("Linux LDN relay disconnected")
        for _ in range(32):
            try:
                readable = select.select([self.connection], [], [], 0)[0]
            except (OSError, ValueError) as exc:
                self.closed = True
                if messages:
                    break
                raise RelayDisconnected("relay connection is no longer usable") from exc
            if not readable:
                break
            try:
                data = self.connection.recv(65536)
            except (BlockingIOError, socket.timeout):
                break
            except OSError as exc:
                self.closed = True
                if messages:
                    break
                raise RelayDisconnected("relay connection closed while receiving") from exc
            if not data:
                self.peer_eof = True
                if self.buffer:
                    raise RelayProtocolError("relay closed with an incomplete JSON message")
                if messages:
                    break
                raise RelayDisconnected("Linux LDN relay disconnected")
            self._feed(data)
            while self.pending:
                messages.append(self.pending.popleft())
        return tuple(messages)

    def close(self) -> None:
        self.closed = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()


def _message_type(message: dict[str, object]) -> str:
    value = message.get("type")
    if not isinstance(value, str):
        raise RelayProtocolError("relay message has no string type")
    return value


def _bounded_int(
    message: dict[str, object], name: str, minimum: int, maximum: int
) -> int:
    value = message.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RelayProtocolError(f"relay field {name!r} must be an integer")
    if not minimum <= value <= maximum:
        raise RelayProtocolError(f"relay field {name!r} is out of range")
    return value


def _hex_field(
    message: dict[str, object],
    name: str,
    *,
    size: int | None = None,
    maximum: int | None = None,
) -> bytes:
    value = message.get(name)
    if not isinstance(value, str):
        raise RelayProtocolError(f"relay field {name!r} must be hexadecimal text")
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise RelayProtocolError(f"relay field {name!r} is not hexadecimal") from exc
    if size is not None and len(raw) != size:
        raise RelayProtocolError(
            f"relay field {name!r} must contain exactly {size} bytes"
        )
    if maximum is not None and len(raw) > maximum:
        raise RelayProtocolError(f"relay field {name!r} is too large")
    return raw


def _ipv4_field(message: dict[str, object], name: str) -> str:
    value = message.get(name)
    if not isinstance(value, str):
        raise RelayProtocolError(f"relay field {name!r} must be an IPv4 address")
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise RelayProtocolError(f"relay field {name!r} is not an IPv4 address") from exc
    return str(address)


def _parse_start(message: dict[str, object]) -> RelayStartRequest:
    if _message_type(message) != "start":
        raise RelayProtocolError("first relay message must be type 'start'")
    version = _bounded_int(message, "version", 0, 0xFFFF)
    if version != RELAY_PROTOCOL_VERSION:
        raise RelayProtocolError(
            f"unsupported relay protocol version {version}; expected {RELAY_PROTOCOL_VERSION}"
        )
    if message.get("profile") != ZA_PROFILE.title_id:
        raise RelayProtocolError("relay profile does not match Pokemon Legends: Z-A")
    nickname = message.get("nickname")
    if not isinstance(nickname, str) or not nickname or len(nickname) > 20:
        raise RelayProtocolError("relay nickname must contain 1 through 20 characters")
    try:
        validate_ldn_nickname(nickname)
    except (TypeError, ValueError) as exc:
        raise RelayProtocolError(str(exc)) from exc
    exact = message.get("require_exact_network")
    if exact is not True:
        raise RelayProtocolError("Z-A relay requires exact room matching")
    expected = _bounded_int(message, "expected_max_participants", 1, 8)
    if expected != ZA_BOX_TRADE_MAX_PARTICIPANTS:
        raise RelayProtocolError("Z-A BoxTrade relay requires exactly two participants")
    try:
        link_code = validate_link_code(message.get("link_code"))
    except (TypeError, ValueError) as exc:
        raise RelayProtocolError(
            "relay Link Code must contain exactly 8 ASCII digits"
        ) from exc
    return RelayStartRequest(
        nickname,
        _bounded_int(message, "communication_id", 0, 0xFFFFFFFFFFFFFFFF),
        _bounded_int(message, "scene_id", 0, 0xFF),
        expected,
        link_code,
    )


class RelayTransport:
    """Windows-compatible transport backed by a loopback/SSH Linux relay."""

    def __init__(
        self,
        endpoint: str | RelayEndpoint = DEFAULT_RELAY_ENDPOINT,
        *,
        link_code: str | None = None,
        nickname: str = "ZA-Trade",
        communication_id: int | None = None,
        scene_id: int = 1,
        expected_max_participants: int = ZA_BOX_TRADE_MAX_PARTICIPANTS,
        startup_timeout: float = RELAY_HANDSHAKE_TIMEOUT,
        log: Callable[[str], None] = print,
    ) -> None:
        self.endpoint = parse_relay_endpoint(endpoint)
        if self.endpoint.host.lower() != "localhost":
            try:
                relay_ip = ipaddress.IPv4Address(self.endpoint.host)
            except ipaddress.AddressValueError as exc:
                raise ValueError(
                    "relay client endpoint must be loopback; use an SSH port forward"
                ) from exc
            if not relay_ip.is_loopback:
                raise ValueError(
                    "relay client endpoint must be loopback; use an SSH port forward"
                )
        if not isinstance(nickname, str) or not nickname or len(nickname) > 20:
            raise ValueError("nickname must contain 1 through 20 characters")
        self.nickname = validate_ldn_nickname(nickname)
        self.link_code = (
            validate_link_code(link_code) if link_code is not None else None
        )
        self.LOCAL_COMMUNICATION_ID = (
            ZA_PROFILE.title_id_int if communication_id is None else communication_id
        )
        if (
            not isinstance(self.LOCAL_COMMUNICATION_ID, int)
            or isinstance(self.LOCAL_COMMUNICATION_ID, bool)
            or not 0 <= self.LOCAL_COMMUNICATION_ID <= 0xFFFFFFFFFFFFFFFF
        ):
            raise ValueError("communication_id must fit in an unsigned 64-bit integer")
        if not isinstance(scene_id, int) or isinstance(scene_id, bool) or not 0 <= scene_id <= 0xFF:
            raise ValueError("scene_id must fit in an unsigned byte")
        if expected_max_participants != ZA_BOX_TRADE_MAX_PARTICIPANTS:
            raise ValueError("Z-A BoxTrade requires exactly two participants")
        if (
            not isinstance(startup_timeout, (int, float))
            or isinstance(startup_timeout, bool)
            or not math.isfinite(float(startup_timeout))
            or startup_timeout <= 0
        ):
            raise ValueError("startup_timeout must be positive")
        self.SCENE_ID = scene_id
        self.APPLICATION_VERSION = 1
        self.expected_max_participants = expected_max_participants
        self.startup_timeout = float(startup_timeout)
        self.log = log
        self.ssid = b""
        self.our_ip = ""
        self.host_ip = ""
        self.our_mac = b""
        self.host_mac = b""
        self.broadcast = ""
        self.app_data = b""
        self.network_app_version = 0
        self._channel: _JsonChannel | None = None
        self._reader_thread: threading.Thread | None = None
        self._reader_done = threading.Event()
        self._stop_acknowledged = threading.Event()
        self._reader_shutdown = threading.Event()
        self._stop_requested = threading.Event()
        self._state_lock = threading.Lock()
        self._packets: deque[tuple[bytes, str]] = deque(
            maxlen=MAX_RELAY_QUEUE_DATAGRAMS
        )
        self._reader_error: RelayError | None = None
        self._dropped_packets = 0

    def _prepare_reader(self) -> None:
        self._reader_done = threading.Event()
        self._stop_acknowledged = threading.Event()
        self._reader_shutdown = threading.Event()
        self._stop_requested = threading.Event()
        with self._state_lock:
            self._packets.clear()
            self._reader_error = None
            self._dropped_packets = 0

    def _set_reader_error(self, error: RelayError) -> None:
        with self._state_lock:
            if self._reader_error is None:
                self._reader_error = error

    def _queue_packet(self, message: dict[str, object]) -> None:
        source_ip = _ipv4_field(message, "source_ip")
        payload = _hex_field(message, "payload", maximum=MAX_RELAY_DATAGRAM)
        if not payload:
            raise RelayProtocolError("relay packet cannot be empty")
        with self._state_lock:
            if len(self._packets) == MAX_RELAY_QUEUE_DATAGRAMS:
                self._packets.popleft()
                self._dropped_packets += 1
            self._packets.append((payload, source_ip))

    def _read_relay(self, channel: _JsonChannel) -> None:
        try:
            while not self._reader_shutdown.is_set():
                messages = channel.receive_available()
                if not messages:
                    self._reader_shutdown.wait(0.005)
                    continue
                for message in messages:
                    kind = _message_type(message)
                    if kind == "packet":
                        self._queue_packet(message)
                    elif kind == "error":
                        detail = message.get("message")
                        raise RelayError(
                            display_safe(detail, limit=1024)
                            if isinstance(detail, str)
                            else "Linux LDN relay failed"
                        )
                    elif kind == "stopped" and self._stop_requested.is_set():
                        self._stop_acknowledged.set()
                        return
                    else:
                        raise RelayProtocolError(
                            f"unexpected relay message type {kind!r}"
                        )
        except RelayError as exc:
            if not self._stop_requested.is_set():
                self._set_reader_error(exc)
        except Exception as exc:
            if not self._stop_requested.is_set():
                self._set_reader_error(
                    RelayProtocolError(
                        f"relay reader failed safely ({type(exc).__name__})"
                    )
                )
        finally:
            self._reader_done.set()

    def start(
        self, *, cancel_event: CancellationToken | None = None
    ) -> "RelayTransport":
        validate_cancel_event(cancel_event)
        raise_if_cancelled(cancel_event)
        if self._channel is not None:
            raise RelayError("relay transport is already started")
        if self.link_code is None:
            raise ValueError("Z-A relay startup requires an 8-digit Link Code")
        connection: socket.socket | None = None
        channel: _JsonChannel | None = None
        start_request_sent = False
        startup_response_received = False
        server_cleanup_confirmed = False
        try:
            connection = socket.create_connection(
                (self.endpoint.host, self.endpoint.port),
                timeout=min(self.startup_timeout, 1.0 if cancel_event is not None else 10.0),
            )
            try:
                peer_address = ipaddress.ip_address(connection.getpeername()[0])
            except (OSError, ValueError) as exc:
                raise RelayError("could not verify the relay peer address") from exc
            if not peer_address.is_loopback:
                raise RelayError(
                    "relay resolved to a non-loopback peer; use an SSH port forward"
                )
            raise_if_cancelled(cancel_event)
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection.settimeout(self.startup_timeout)
            channel = _JsonChannel(connection)
            channel.send(
                {
                    "type": "start",
                    "version": RELAY_PROTOCOL_VERSION,
                    "profile": ZA_PROFILE.title_id,
                    "nickname": self.nickname,
                    "communication_id": self.LOCAL_COMMUNICATION_ID,
                    "scene_id": self.SCENE_ID,
                    "link_code": self.link_code,
                    "require_exact_network": True,
                    "expected_max_participants": self.expected_max_participants,
                }
            )
            start_request_sent = True
            response = channel.receive_one(
                self.startup_timeout, cancel_event=cancel_event
            )
            startup_response_received = True
            raise_if_cancelled(cancel_event)
            kind = _message_type(response)
            if kind == "error":
                # The server sends its official startup error only after the
                # Linux transport has been stopped successfully.
                server_cleanup_confirmed = True
                detail = response.get("message")
                raise RelayError(
                    display_safe(detail, limit=1024)
                    if isinstance(detail, str)
                    else "Linux LDN relay failed"
                )
            if kind != "started":
                raise RelayProtocolError("relay did not return a started message")
            version = _bounded_int(response, "version", 0, 0xFFFF)
            if version != RELAY_PROTOCOL_VERSION:
                raise RelayProtocolError("relay protocol version changed during startup")
            if response.get("profile") != ZA_PROFILE.title_id:
                raise RelayProtocolError("relay started the wrong title profile")
            self.ssid = _hex_field(response, "ssid", size=16)
            self.our_ip = _ipv4_field(response, "our_ip")
            self.host_ip = _ipv4_field(response, "host_ip")
            self.our_mac = _hex_field(response, "our_mac", size=6)
            self.host_mac = _hex_field(response, "host_mac", size=6)
            self.broadcast = _ipv4_field(response, "broadcast")
            self.app_data = _hex_field(response, "application_data", maximum=0x200)
            if not advertised_link_code_matches(self.app_data, self.link_code):
                raise RelayProtocolError(
                    "relay joined a room with a different or malformed Link Code"
                )
            self.network_app_version = _bounded_int(
                response, "application_version", 0, 0xFFFF
            )
            if self.network_app_version != self.APPLICATION_VERSION:
                raise RelayProtocolError(
                    "relay joined an unsupported Z-A application version"
                )
            returned_comm_id = _bounded_int(
                response, "communication_id", 0, 0xFFFFFFFFFFFFFFFF
            )
            returned_scene = _bounded_int(response, "scene_id", 0, 0xFF)
            if returned_comm_id != self.LOCAL_COMMUNICATION_ID or returned_scene != self.SCENE_ID:
                raise RelayProtocolError("relay joined a different LDN room profile")
            self.APPLICATION_VERSION = self.network_app_version
            connection.settimeout(RELAY_IO_TIMEOUT)
            self._prepare_reader()
            self._channel = channel
            self.log(
                f"[relay] connected to {self.endpoint}; "
                f"LDN us={self.our_ip}, host={self.host_ip}"
            )
            reader = threading.Thread(
                target=self._read_relay,
                args=(channel,),
                name="za-relay-reader",
                daemon=True,
            )
            self._reader_thread = reader
            reader.start()
            return self
        except BaseException as exc:
            # Once a start request has reached Linux, cancellation and local
            # handshake failures are protocol operations rather than a local
            # socket close.  Waiting for ``stopped`` guarantees that the relay
            # has restored its radio before a Windows GUI worker may finish.
            # An official server ``error`` already carries that guarantee.
            remote_cleanup_error: RelayError | None = None
            if (
                channel is not None
                and start_request_sent
                and not server_cleanup_confirmed
            ):
                cancelled = isinstance(exc, OperationCancelled)
                first_response_timeout = (
                    RELAY_CANCEL_NEGOTIATION_TIMEOUT
                    if cancelled and not startup_response_received
                    else RELAY_STOP_TIMEOUT
                )
                try:
                    channel.send({"type": "stop"})
                    first_response_deadline = (
                        time.monotonic() + first_response_timeout
                    )
                    response = channel.receive_one(first_response_timeout)
                    response_kind = _message_type(response)
                    cleanup_deadline = (
                        first_response_deadline
                        if first_response_timeout == RELAY_STOP_TIMEOUT
                        else time.monotonic() + RELAY_STOP_TIMEOUT
                    )
                    while response_kind in {"started", "stopping", "packet"}:
                        remaining = cleanup_deadline - time.monotonic()
                        if remaining <= 0:
                            raise RelayError(
                                "timed out waiting for Linux radio cleanup"
                            )
                        response = channel.receive_one(remaining)
                        response_kind = _message_type(response)
                    if response_kind not in {"stopped", "error"}:
                        raise RelayProtocolError(
                            "relay did not acknowledge failed startup cleanup"
                        )
                    server_cleanup_confirmed = True
                except RelayError:
                    cleanup_context = (
                        "cancellation" if cancelled else "startup failure"
                    )
                    remote_cleanup_error = RelayError(
                        "Linux relay did not confirm radio cleanup after "
                        f"{cleanup_context}"
                    )
                    try:
                        self.log(
                            f"[relay] {cleanup_context} cleanup was not confirmed"
                        )
                    except Exception:
                        pass
            self._stop_requested.set()
            self._reader_shutdown.set()
            self._channel = None
            if channel is not None:
                channel.close()
            elif connection is not None:
                connection.close()
            reader = self._reader_thread
            if reader is not None and reader.is_alive():
                reader.join(timeout=RELAY_STOP_TIMEOUT)
            self._reader_thread = None
            if remote_cleanup_error is not None:
                raise remote_cleanup_error from exc
            raise

    def recv(self) -> list[tuple[bytes, str]]:
        if self._channel is None:
            raise RelayError("relay transport is not started")
        with self._state_lock:
            packets = list(self._packets)
            self._packets.clear()
            dropped = self._dropped_packets
            self._dropped_packets = 0
            error = self._reader_error if not packets else None
        if dropped:
            self.log(
                f"[relay] dropped {dropped} queued datagram(s) while the client was busy"
            )
        if error is not None:
            raise error
        return packets

    def send(self, datagram: bytes, dst_ip: str) -> None:
        if self._channel is None:
            raise RelayError("relay transport is not started")
        payload = bytes(datagram)
        if not payload or len(payload) > MAX_RELAY_DATAGRAM:
            raise ValueError(
                f"relay datagram must contain 1 through {MAX_RELAY_DATAGRAM} bytes"
            )
        destination = str(ipaddress.IPv4Address(dst_ip))
        if destination not in (self.host_ip, self.broadcast, "255.255.255.255"):
            raise ValueError("relay destination must be the joined host or broadcast")
        self._channel.send(
            {
                "type": "send",
                "destination_ip": destination,
                "payload": payload.hex(),
            }
        )

    def stop(self) -> None:
        channel, self._channel = self._channel, None
        if channel is None:
            return
        self._stop_requested.set()
        cleanup_error: RelayError | None = None
        try:
            channel.send({"type": "stop"})
            if not self._reader_done.wait(RELAY_STOP_TIMEOUT):
                cleanup_error = RelayError(
                    "timed out waiting for Linux radio cleanup acknowledgement"
                )
            elif not self._stop_acknowledged.is_set():
                cleanup_error = RelayError(
                    "Linux relay disconnected without confirming radio cleanup"
                )
        except RelayError as exc:
            cleanup_error = exc
        finally:
            self._reader_shutdown.set()
            channel.close()
            reader, self._reader_thread = self._reader_thread, None
            if reader is not None and reader.is_alive():
                reader.join(timeout=RELAY_STOP_TIMEOUT)
        if cleanup_error is not None:
            self.log(f"[relay] cleanup failed: {cleanup_error}")
            raise cleanup_error


TransportFactory = Callable[[RelayStartRequest], DatagramTransport]


class ZARelayServer:
    """Single-client, single-session loopback relay server."""

    def __init__(
        self,
        *,
        keys_path: str | None,
        listen: str | RelayEndpoint = DEFAULT_RELAY_ENDPOINT,
        phyname: str = "phy0",
        ifname: str = "ldnclient",
        single_interface: bool | None = None,
        bss_helper: str | None = None,
        transport_factory: TransportFactory | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.listen = parse_relay_endpoint(listen, allow_zero_port=True)
        try:
            listen_ip = ipaddress.IPv4Address(self.listen.host)
        except ipaddress.AddressValueError as exc:
            raise ValueError("relay server listen address must be an IPv4 literal") from exc
        if not listen_ip.is_loopback:
            raise ValueError(
                "relay server may only listen on IPv4 loopback; use an SSH port forward"
            )
        if transport_factory is None:
            if os.name == "nt":
                raise RuntimeError("relay-serve requires Linux or WSL2")
            if not keys_path:
                raise ValueError("relay-serve requires keys_path on Linux")
        self.keys_path = keys_path
        self.phyname = phyname
        self.ifname = ifname
        if single_interface is not None and not isinstance(single_interface, bool):
            raise TypeError("single_interface must be a bool or None")
        self.single_interface = single_interface
        if bss_helper is not None and transport_factory is not None:
            raise ValueError("BSS helper cannot be combined with an injected transport")
        self.bss_helper = bss_helper
        self.transport_factory = transport_factory or self._local_transport
        self.log = log
        self.listener: socket.socket | None = None
        self.bound_endpoint: RelayEndpoint | None = None
        self._failed_transport: DatagramTransport | None = None
        self._fatal_cleanup_error: RelayCleanupError | None = None

    def _local_transport(self, request: RelayStartRequest) -> DatagramTransport:
        from zasim.ldn.transport import LiveTransport

        return make_local_transport(
            LiveTransport, link_code=request.link_code,
            communication_id=request.communication_id, scene_id=request.scene_id,
            expected_max_participants=request.expected_max_participants,
            nickname=request.nickname, keys_path=self.keys_path,
            phyname=self.phyname, ifname=self.ifname, log=self.log,
            single_interface=self.single_interface,
            **({"bss_helper": self.bss_helper} if self.bss_helper is not None else {}),
        )

    def open(self) -> RelayEndpoint:
        if self.listener is not None:
            raise RelayError("relay server is already open")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.listen.host, self.listen.port))
            listener.listen(1)
            host, port = listener.getsockname()[:2]
            endpoint = RelayEndpoint(str(host), int(port))
        except BaseException:
            listener.close()
            raise
        self.listener = listener
        self.bound_endpoint = endpoint
        return self.bound_endpoint

    @staticmethod
    def _started_message(transport: DatagramTransport) -> dict[str, object]:
        application_version = getattr(transport, "network_app_version", None)
        if application_version is None:
            application_version = transport.APPLICATION_VERSION
        return {
            "type": "started",
            "version": RELAY_PROTOCOL_VERSION,
            "profile": ZA_PROFILE.title_id,
            "ssid": bytes(transport.ssid).hex(),
            "our_ip": transport.our_ip,
            "host_ip": transport.host_ip,
            "our_mac": bytes(transport.our_mac).hex(),
            "host_mac": bytes(transport.host_mac).hex(),
            "broadcast": transport.broadcast,
            "application_data": bytes(transport.app_data or b"").hex(),
            "application_version": int(application_version),
            "communication_id": int(transport.LOCAL_COMMUNICATION_ID),
            "scene_id": int(transport.SCENE_ID),
        }

    def serve_once(self) -> dict[str, int | str]:
        if self._fatal_cleanup_error is not None:
            raise self._fatal_cleanup_error
        if self.listener is None:
            self.open()
        assert self.listener is not None
        connection, peer = self.listener.accept()
        transport: DatagramTransport | None = None
        received = sent = 0
        channel = _JsonChannel(connection)
        clean_stop = False
        primary_error: BaseException | None = None
        transport_stop_attempted = False

        def summary() -> dict[str, int | str]:
            return {
                "listen": str(self.bound_endpoint),
                "packets_to_windows": received,
                "packets_from_windows": sent,
            }

        def stop_transport_once() -> None:
            nonlocal transport, transport_stop_attempted
            if transport is None or transport_stop_attempted:
                return
            transport_stop_attempted = True
            owned_transport = transport
            try:
                owned_transport.stop()
            except BaseException as exc:
                self._failed_transport = owned_transport
                transport = None
                fatal = cleanup_failure("Linux transport cleanup", exc)
                raise fatal from exc
            transport = None

        def report_secondary_failure(
            primary: BaseException, phase: str, secondary: BaseException
        ) -> None:
            note = (
                f"relay {phase} also failed ({type(secondary).__name__}): "
                f"{display_safe(secondary, limit=512)}"
            )
            add_note = getattr(primary, "add_note", None)
            if add_note is not None:
                add_note(note)
            try:
                self.log(f"[relay] {note}")
            except Exception:
                pass

        def cleanup_failure(
            phase: str, cleanup_error: BaseException
        ) -> RelayCleanupError:
            fatal = RelayCleanupError(
                f"{phase} failed; relay cannot safely accept another session",
                cleanup_error=cleanup_error,
                primary_error=primary_error,
            )
            self._fatal_cleanup_error = fatal
            if primary_error is not None:
                report_secondary_failure(
                    fatal, "original session", primary_error
                )
            return fatal

        try:
            # Accepted descriptors already need cleanup, even when socket
            # configuration fails before the first protocol/transport action.
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection.settimeout(RELAY_IO_TIMEOUT)
            peer_ip = ipaddress.IPv4Address(peer[0])
            if not peer_ip.is_loopback:
                raise RelayProtocolError("relay client is not connected through loopback")
            request = _parse_start(channel.receive_one(10.0))
            startup_cancel = threading.Event()
            startup_monitor_stop = threading.Event()
            startup_stop_requested = threading.Event()
            startup_disconnected = threading.Event()
            startup_errors: list[BaseException] = []

            def monitor_startup_client() -> None:
                while not startup_monitor_stop.wait(0.01):
                    try:
                        commands = channel.receive_available()
                    except RelayDisconnected:
                        startup_disconnected.set()
                        startup_cancel.set()
                        return
                    except BaseException as exc:
                        startup_errors.append(exc)
                        startup_cancel.set()
                        return
                    for command in commands:
                        try:
                            kind = _message_type(command)
                        except BaseException as exc:
                            startup_errors.append(exc)
                            startup_cancel.set()
                            return
                        if kind == "stop":
                            try:
                                channel.send({"type": "stopping"})
                            except RelayDisconnected:
                                startup_disconnected.set()
                                startup_cancel.set()
                                return
                            startup_stop_requested.set()
                            startup_cancel.set()
                            return
                        startup_errors.append(
                            RelayProtocolError(
                                f"unexpected client relay message type {kind!r} during startup"
                            )
                        )
                        startup_cancel.set()
                        return

            try:
                transport = self.transport_factory(request)
                connection.settimeout(0.1)
                startup_monitor = threading.Thread(
                    target=monitor_startup_client,
                    name="za-relay-startup-monitor",
                    daemon=True,
                )
                startup_monitor.start()
                startup_error: BaseException | None = None
                try:
                    start_transport(transport, startup_cancel)
                    raise_if_cancelled(startup_cancel)
                except BaseException as exc:
                    startup_error = exc
                finally:
                    startup_monitor_stop.set()
                    startup_monitor.join(timeout=1.0)
                if startup_monitor.is_alive():
                    raise RelayError("relay startup monitor did not stop safely")
                if startup_stop_requested.is_set():
                    stop_transport_once()
                    channel.send({"type": "stopped"})
                    return summary()
                if startup_disconnected.is_set():
                    raise RelayDisconnected(
                        "Windows client disconnected during Linux LDN startup"
                    )
                if startup_errors:
                    raise startup_errors[0]
                if startup_error is not None:
                    raise startup_error
            except BaseException as exc:
                if isinstance(exc, (RelayDisconnected, RelayCleanupError)):
                    raise
                detail = (
                    f"Linux LDN startup failed ({type(exc).__name__}); "
                    "see the relay console"
                )
                self.log(f"[relay] {detail}: {exc}")
                startup_failure = RelayError(detail)
                startup_failure.__cause__ = exc
                primary_error = startup_failure
                # A normal startup error tells the Windows worker that the
                # operation is over.  Do not release it until the Linux
                # transport has restored the radio.  If restoration fails,
                # stop the relay service without first sending a misleading
                # recoverable error to the client.
                stop_transport_once()
                try:
                    channel.send({"type": "error", "message": detail})
                except RelayError:
                    pass
                raise startup_failure from exc
            channel.send(self._started_message(transport))
            connection.settimeout(RELAY_IO_TIMEOUT)
            self.log(f"[relay] Windows client connected from {peer[0]}:{peer[1]}")

            running = True
            try:
                while running:
                    worked = False
                    commands = channel.receive_available()
                    for command in commands:
                        worked = True
                        kind = _message_type(command)
                        if kind == "stop":
                            clean_stop = True
                            running = False
                            break
                        if kind != "send":
                            raise RelayProtocolError(
                                f"unexpected client relay message type {kind!r}"
                            )
                        destination = _ipv4_field(command, "destination_ip")
                        if destination not in (
                            transport.host_ip,
                            transport.broadcast,
                            "255.255.255.255",
                        ):
                            raise RelayProtocolError(
                                "client requested a destination outside the joined LDN room"
                            )
                        payload = _hex_field(
                            command, "payload", maximum=MAX_RELAY_DATAGRAM
                        )
                        if not payload:
                            raise RelayProtocolError(
                                "client relay datagram cannot be empty"
                            )
                        transport.send(payload, destination)
                        sent += 1
                    if not running:
                        break
                    for payload, source_ip in transport.recv():
                        worked = True
                        datagram = bytes(payload)
                        if not datagram or len(datagram) > MAX_RELAY_DATAGRAM:
                            raise RelayProtocolError(
                                "Linux transport returned an invalid Pia datagram"
                            )
                        source = str(ipaddress.IPv4Address(source_ip))
                        channel.send(
                            {
                                "type": "packet",
                                "source_ip": source,
                                "payload": datagram.hex(),
                            }
                        )
                        received += 1
                    if not worked:
                        time.sleep(0.005)
            except RelayDisconnected:
                raise RelayDisconnected(
                    "Windows relay client disconnected before a clean stop"
                )
            if clean_stop:
                # A stopped acknowledgement means cleanup is complete, not
                # merely scheduled.  This ordering is relied on by GUI close.
                stop_transport_once()
                channel.send({"type": "stopped"})
            return summary()
        except RelayProtocolError as exc:
            primary_error = exc
            # Returning an error releases the Windows worker.  Restore the
            # Linux radio first so the client cannot start another operation
            # while cleanup is still in progress.  A cleanup failure must
            # fail-stop the relay without advertising a recoverable protocol
            # error to Windows.
            stop_transport_once()
            try:
                channel.send({"type": "error", "message": str(exc)})
            except RelayError:
                pass
            raise
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            fatal_error = (
                primary_error
                if isinstance(primary_error, RelayCleanupError)
                else None
            )
            try:
                stop_transport_once()
            except RelayCleanupError as exc:
                fatal_error = exc
            except BaseException as exc:
                fatal_error = cleanup_failure("Linux transport cleanup", exc)
            try:
                channel.close()
            except BaseException as exc:
                if fatal_error is None:
                    fatal_error = cleanup_failure("relay channel close", exc)
                else:
                    report_secondary_failure(fatal_error, "channel close", exc)
            if fatal_error is not None and fatal_error is not primary_error:
                raise fatal_error from fatal_error.cleanup_error

    def close(self) -> None:
        listener, self.listener = self.listener, None
        self.bound_endpoint = None
        if listener is not None:
            listener.close()


def serve_za_relay(
    *,
    keys_path: str,
    listen: str | RelayEndpoint = DEFAULT_RELAY_ENDPOINT,
    phyname: str = "phy0",
    ifname: str = "ldnclient",
    single_interface: bool | None = None,
    bss_helper: str | None = None,
    once: bool = False,
    log: Callable[[str], None] = print,
) -> dict[str, int | str]:
    """Serve sequential Windows sessions, or one session when ``once`` is set."""

    if not isinstance(once, bool):
        raise TypeError("once must be a bool")

    server = ZARelayServer(
        keys_path=keys_path,
        listen=listen,
        phyname=phyname,
        ifname=ifname,
        single_interface=single_interface,
        **({"bss_helper": bss_helper} if bss_helper is not None else {}),
        log=log,
    )
    endpoint = server.open()
    log(f"[relay] listening on {endpoint} (loopback only; use SSH forwarding)")
    try:
        sessions = 0
        while True:
            try:
                result = server.serve_once()
            except RelayCleanupError:
                raise
            except RelayError as exc:
                if once:
                    raise
                log(f"[relay] session aborted safely: {display_safe(exc, limit=1024)}")
                log("[relay] waiting for the next Windows client")
                continue
            sessions += 1
            if once:
                return result
            log(
                f"[relay] session {sessions} finished; radio restored; "
                "waiting for the next Windows client"
            )
    finally:
        server.close()


__all__ = [
    "DEFAULT_RELAY_ENDPOINT",
    "DEFAULT_RELAY_HOST",
    "DEFAULT_RELAY_PORT",
    "DatagramTransport",
    "MAX_RELAY_DATAGRAM",
    "RELAY_PROTOCOL_VERSION",
    "RelayCleanupError",
    "RelayDisconnected",
    "RelayEndpoint",
    "RelayError",
    "RelayProtocolError",
    "RelayStartRequest",
    "RelayTransport",
    "ZARelayServer",
    "parse_relay_endpoint",
    "serve_za_relay",
]
