"""Active Pokemon Legends: Z-A local-trade client core.

The reliable and title layers are pure, clock-driven state machines.  The
optional :func:`run_live_trade` adapter either joins LDN on Linux or uses the
loopback/SSH Linux relay from Windows.  It has not been validated against
physical Switch hardware.  Automatic agreement is disabled unless the caller
explicitly opts in.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, replace
import hashlib
import math
import os
from pathlib import Path
import queue
import stat
import threading
import tempfile
import time
from typing import Callable

from zasim.ldn.transport import LiveTransport

from .compatibility import DEFAULT_ZA_TITLE_VERSION, identification_token_for_title
from .broadcast import BroadcastReceiver, BroadcastSender
from .peer_setup import PeerSetup, decode as decode_peer_setup
from .user_data import UserData
from .sync_save import SyncSavePeer, decode as decode_sync_save
from .transport_setup import make_local_transport
from .control import (
    CancellationToken,
    OperationCancelled,
    raise_if_cancelled,
    start_transport,
    validate_cancel_event,
)
from .join_observation import JoinObservation
from .link_code import advertised_link_code_matches, validate_link_code
from .pa9 import PA9
from .file_save import (
    FileIdentity, _absolute_path, file_identity, exclusive_save_lock,
    publish_approved, _sync_output_directory, _cleanup_owned_temporary,
)
from .pia import (
    PACKET_FLAG_SKIP_DESTINATION_CHECK,
    PACKET_FLAG_SKIP_NETWORK_CHECK,
    DecodedDatagram,
    PiaCrypto,
    PiaHeader,
    PiaMessage,
    build_message,
    decode_datagram,
    encode_datagram,
)
from .profile import ZA_PROFILE
from .relay import DatagramTransport, RelayTransport
from .reliable import (
    BulkAckEntry,
    ReliableFlags,
    ReliableFrame,
    ReliableReassembler,
    build_bulk_ack,
    parse_bulk_ack,
    parse_reliable,
)
from .session import OutboundPiaMessage, ZASession
from .trade import IncomingResult, TradeNegotiation, TradeState
from .trade_protocol import (
    COMPATIBILITY_SIZE,
    PARTY_PA9_SIZE,
    TRADE_ENDPOINT,
    CompatibilityMatrix,
    TradeCommandID,
    TradeFrame,
    decode_frame,
)


PROTO_RELIABLE = 10
ZA_RELIABLE_PORT = 0
ZA_RELIABLE_PROTOCOL_SPECIFIC = 0xFD
BULK_ACK_FLAGS = 0
INITIAL_RELIABLE_SEQUENCE = 0xFFF0
MAX_RECEIVED_COMMAND_HISTORY = 256
DATA_FLAGS = (
    ReliableFlags.APPLICATION_DATA
    | ReliableFlags.MESSAGE_START
    | ReliableFlags.MESSAGE_END
)
INITIAL_DATA_FLAGS = DATA_FLAGS | ReliableFlags.INITIALIZED
ACK_MESSAGE_FLAGS = 0x40
DATA_MESSAGE_FLAGS = 0x20


class ClientProtocolError(RuntimeError):
    """Authenticated input violates the recovered Z-A trade protocol."""


class ReliableTimeout(TimeoutError):
    """A reliable application frame exceeded its retry budget."""


class TradeDeclined(RuntimeError):
    """The confirmation policy rejected the received offer."""


@dataclass(frozen=True, slots=True)
class ReliableOutbound:
    """One complete protocol-10 payload and its Compact Message metadata."""

    payload: bytes
    protocol_specific: int = ZA_RELIABLE_PROTOCOL_SPECIFIC
    message_flags: int | None = DATA_MESSAGE_FLAGS
    port: int = ZA_RELIABLE_PORT
    retransmission: bool = False


@dataclass(slots=True)
class _PendingSend:
    sequence: int
    payload: bytes
    flags: ReliableFlags
    sent_at: float
    attempts: int = 1


def _clock(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("now must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("now must be a finite non-negative number")
    return result


class ReliableTradeEngine:
    """Pure Z-A protocol-10 send window and BoxTrade endpoint.

    The outer Compact Message identifier is the recovered fixed tuple
    ``protocol=10, port=0, protocol_specific=0xFD``.  The runtime station index
    used for peer routing is a separate Pia value and must not replace 0xFD.
    """

    def __init__(
        self,
        *,
        offer: bytes | bytearray | memoryview | None = None,
        compatibility: CompatibilityMatrix | None = None,
        auto_confirm: bool = False,
        retransmit_timeout: float = 0.5,
        max_attempts: int = 8,
        initial_sequence: int = INITIAL_RELIABLE_SEQUENCE,
    ) -> None:
        if compatibility is None:
            compatibility = CompatibilityMatrix(bytes(COMPATIBILITY_SIZE))
        if not isinstance(compatibility, CompatibilityMatrix):
            raise TypeError("compatibility must be a CompatibilityMatrix")
        if not isinstance(auto_confirm, bool):
            raise TypeError("auto_confirm must be a bool")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool):
            raise TypeError("max_attempts must be an integer")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.retransmit_timeout = _clock(retransmit_timeout)
        if self.retransmit_timeout == 0:
            raise ValueError("retransmit_timeout must be positive")
        if not 0 <= initial_sequence <= 0xFFFF:
            raise ValueError("initial_sequence must fit in u16")

        self.negotiation = TradeNegotiation(compatibility)
        self.auto_confirm = auto_confirm
        self._offer: bytes | None = None
        self.set_offer(offer)
        self._send_next = initial_sequence
        self._send_origin = initial_sequence
        self._receive = ReliableReassembler(next_sequence=initial_sequence)
        self._pending: OrderedDict[int, _PendingSend] = OrderedDict()
        self._outbox: deque[ReliableOutbound] = deque()
        self._ack_owed = False
        self._sent_application = False
        self._now = 0.0
        self.max_attempts = max_attempts
        self.received_commands: deque[IncomingResult] = deque(
            maxlen=MAX_RECEIVED_COMMAND_HISTORY
        )
        self.other_application_handler = None
        self.title_start_guard = None
        self.title_enabled = True
        self._held_title_frames = deque()
        self._native_selection_barrier = False

    @property
    def remote_offer(self) -> bytes | None:
        """Return the last committed (non-transient) remote selection.

        The native receive buffer is intentionally updated by transient
        SelectPokemon messages as well.  Exposing that buffer here would let a
        later clear/preview message replace the PA9 that the live adapter asks
        the user to approve or writes after a completed trade.
        """

        return self.negotiation.remote_committed_offer

    @property
    def complete(self) -> bool:
        return (
            self.negotiation.local_state is TradeState.FINAL_AGREEMENT
            and self.negotiation.remote_state is TradeState.FINAL_AGREEMENT
        )

    @property
    def send_next(self) -> int:
        return self._send_next

    @property
    def send_lowest_pending(self) -> int:
        return next(iter(self._pending), self._send_next)

    @property
    def unacknowledged(self) -> tuple[int, ...]:
        return tuple(self._pending)

    def set_offer(
        self, offer: bytes | bytearray | memoryview | None
    ) -> None:
        if offer is None:
            self._offer = None
            return
        raw = bytes(offer)
        if len(raw) != PARTY_PA9_SIZE:
            raise ValueError(
                f"offer must be a 0x{PARTY_PA9_SIZE:X}-byte encrypted party PA9"
            )
        self._offer = raw

    def feed_message(self, message: PiaMessage, *, now: float) -> None:
        """Consume one decoded Compact Message and queue reactions."""

        if not isinstance(message, PiaMessage):
            raise TypeError("message must be a PiaMessage")
        if message.proto != PROTO_RELIABLE:
            return
        if message.port != ZA_RELIABLE_PORT:
            return
        if message.protocol_specific != ZA_RELIABLE_PROTOCOL_SPECIFIC:
            raise ClientProtocolError(
                "Z-A trade Reliable protocol-specific id must be 0xFD; got "
                f"0x{message.protocol_specific:02X}"
            )
        self.feed_reliable(message.payload, now=now)

    def feed_reliable(
        self, payload: bytes | bytearray | memoryview, *, now: float
    ) -> None:
        """Consume an already-demultiplexed protocol-10 frame."""

        self._now = _clock(now)
        frame = parse_reliable(payload)
        if frame.flags & (ReliableFlags.RESET | ReliableFlags.RESET_ACK):
            raise ClientProtocolError(
                "Reliable reset negotiation is not recovered for the active trade path"
            )
        if frame.is_application_data:
            if frame.flags & ReliableFlags.COMPRESSED:
                raise ClientProtocolError(
                    "compressed Reliable application messages are not supported"
                )
            self._ack_owed = True
            for complete in self._receive.feed(frame):
                # A Pia Reliable stream can carry application endpoints other
                # than BoxTrade.  They share sequencing/ACKs but are not title
                # commands for this state machine.
                if not complete.payload or complete.payload[0] != TRADE_ENDPOINT:
                    if self.other_application_handler is not None:
                        self.other_application_handler(complete.payload)
                    continue
                title_frame = TradeFrame.from_wire_payload(complete.payload)
                if not self.title_enabled:
                    decode_frame(title_frame)  # validate the complete real frame
                    if not self._held_title_frames and title_frame.command_id != TradeCommandID.READY:
                        raise ClientProtocolError('title_before_native_ready')
                    if len(self._held_title_frames) >= 8:
                        raise ClientProtocolError('held_title_frame_limit')
                    self._held_title_frames.append(title_frame)
                    continue
                result = self.negotiation.receive(title_frame, now=self._now)
                self.received_commands.append(result)
        elif frame.payload:
            acknowledgement = parse_bulk_ack(frame.payload)
            for entry in acknowledgement.entries:
                self._apply_ack(entry)
        self.advance(self._now)

    def _apply_ack(self, entry: BulkAckEntry) -> None:
        for sequence in tuple(self._pending):
            if entry.acknowledges(sequence):
                del self._pending[sequence]

    def _queue_action_payload(self, payload: bytes) -> None:
        sequence = self._send_next
        self._send_next = (self._send_next + 1) & 0xFFFF
        flags = INITIAL_DATA_FLAGS if not self._sent_application else DATA_FLAGS
        self._sent_application = True
        pending = _PendingSend(sequence, payload, flags, self._now)
        self._pending[sequence] = pending
        self._outbox.append(
            ReliableOutbound(
                ReliableFrame(
                    flags,
                    sequence,
                    self.send_lowest_pending,
                    payload,
                ).pack(),
                ZA_RELIABLE_PROTOCOL_SPECIFIC,
            )
        )

    def _collect_actions(self) -> None:
        for action in self.negotiation.drain_actions():
            self._queue_action_payload(action.frame.wire_payload)

    def queue_peer_setup(self, payload: bytes, *, now: float) -> None:
        decode_peer_setup(payload)
        self._now = _clock(now)
        self._queue_action_payload(payload)

    def queue_sync_save(self, payload: bytes, *, now: float) -> None:
        message = decode_sync_save(payload)
        if not self.complete or message.command.value != 0:
            raise ClientProtocolError('sync_save_without_both_final_or_wrong_role')
        self._now = _clock(now)
        self._queue_action_payload(payload)

    def hold_title_until_native_ready(self, guard) -> None:
        if self._sent_application or self.negotiation.ready_sent or not callable(guard):
            raise ClientProtocolError('title_hold_must_precede_application_send')
        self.title_enabled = False
        self.title_start_guard = guard
        self._native_selection_barrier = True

    def _try_start_title(self) -> None:
        if self.title_enabled or not self._held_title_frames:
            return
        if self.title_start_guard is None or not self.title_start_guard():
            return
        self.title_enabled = True
        while self._held_title_frames:
            result = self.negotiation.receive(self._held_title_frames.popleft(), now=self._now)
            self.received_commands.append(result)

    def advance(self, now: float) -> None:
        """Advance title time and enqueue newly enabled application commands."""

        self._now = _clock(now)
        self._try_start_title()
        if not self.title_enabled:
            return
        self.negotiation.advance(self._now)
        if (
            self._offer is not None
            and self.negotiation.local_state is TradeState.SELECTING
        ):
            if (
                self._native_selection_barrier
                and self.negotiation.remote_offer is None
            ):
                # A native Ready can precede its next initial-sync tick. That
                # tick deletes premature committed offers (0x95f6d8), but not
                # previews. Native Select (including preview) is emitted only
                # from state2 (0x960ad0), providing an actual readiness signal
                # rather than a guessed delay or transport-ACK assumption.
                if self.negotiation.local_offer is None:
                    self.negotiation.select(self._offer, transient=True)
            elif (
                self.negotiation.local_offer is None
                or self._native_selection_barrier
            ):
                self.negotiation.select(self._offer)
        if (
            self.auto_confirm
            and self.remote_offer is not None
            and self.negotiation.local_state is TradeState.OFFERED
            # A repaired Reliable gap can deliver Select + Confirm in one
            # feed. The offer remains actionable after the peer confirms it.
            and self.negotiation.remote_state in (
                TradeState.OFFERED, TradeState.CONFIRMED
            )
        ):
            self.negotiation.confirm(now=self._now)
        self.negotiation.advance(self._now)
        self._collect_actions()

    def confirm(self, *, now: float) -> None:
        """Explicitly confirm the selected exchange."""

        self._now = _clock(now)
        self.negotiation.confirm(now=self._now)
        self._collect_actions()

    def cancel(self, reason: int = 0, *, now: float) -> None:
        self._now = _clock(now)
        self.negotiation.cancel(reason)
        self._collect_actions()

    def _queue_retransmissions(self) -> None:
        for pending in self._pending.values():
            if self._now - pending.sent_at < self.retransmit_timeout:
                continue
            if pending.attempts >= self.max_attempts:
                raise ReliableTimeout(
                    f"Reliable sequence 0x{pending.sequence:04X} was not acknowledged "
                    f"after {pending.attempts} attempts"
                )
            pending.attempts += 1
            pending.sent_at = self._now
            wire = ReliableFrame(
                pending.flags,
                pending.sequence,
                self.send_lowest_pending,
                pending.payload,
            ).pack()
            self._outbox.append(
                ReliableOutbound(
                    wire,
                    ZA_RELIABLE_PROTOCOL_SPECIFIC,
                    retransmission=True,
                )
            )

    def _queue_ack(self) -> None:
        if not self._ack_owed:
            return
        next_expected = self._receive.next_sequence
        if next_expected is None:
            return
        selective = BulkAckEntry.from_received(
            next_expected, self._receive.pending_sequences
        )
        ack_payload = build_bulk_ack(
            next_expected, selective.mask, ack_flags=BULK_ACK_FLAGS
        )
        control = ReliableFrame(
            0,
            self._send_origin,
            self.send_lowest_pending,
            ack_payload,
        ).pack()
        self._outbox.append(
            ReliableOutbound(
                control,
                ZA_RELIABLE_PROTOCOL_SPECIFIC,
                message_flags=ACK_MESSAGE_FLAGS,
            )
        )
        self._ack_owed = False

    def drain(self, *, now: float) -> tuple[ReliableOutbound, ...]:
        """Return new sends, due retries, then a coalesced selective ACK."""

        self.advance(now)
        self._queue_retransmissions()
        self._queue_ack()
        result = tuple(self._outbox)
        self._outbox.clear()
        return result


class ZATradeClient:
    """Compose the generic Pia session and the pure Reliable trade engine."""

    def __init__(self, session: ZASession, trade: ReliableTradeEngine, *, peer_user_data: UserData | None = None, sync_save_peer: SyncSavePeer | None = None) -> None:
        if not isinstance(session, ZASession):
            raise TypeError("session must be a ZASession")
        if not isinstance(trade, ReliableTradeEngine):
            raise TypeError("trade must be a ReliableTradeEngine")
        self.session = session
        self.trade = trade
        self.broadcast: BroadcastReceiver | None = None
        self.broadcast_sender: BroadcastSender | None = None
        self.peer_setup = PeerSetup(peer_user_data) if peer_user_data is not None else None
        self.sync_save_peer = sync_save_peer
        self.sync_start_guard = None
        self._receive_now = 0.0
        if self.peer_setup is not None or self.sync_save_peer is not None:
            self.trade.other_application_handler = self._receive_other_application
        if self.peer_setup is not None:
            self.trade.hold_title_until_native_ready(lambda: self.peer_setup.ready_for_title)

    def _receive_other_application(self, payload):
        if payload[:1] == b'\x14' and self.peer_setup is not None:
            self.peer_setup.receive(payload)
        elif payload[:1] == b'\x02' and self.sync_save_peer is not None:
            self.sync_save_peer.receive(payload, now=self._receive_now)

    def feed_decoded(
        self, decoded: DecodedDatagram, *, now: float, tick: int
    ) -> None:
        if not isinstance(decoded, DecodedDatagram):
            raise TypeError("decoded must be a DecodedDatagram")
        self._receive_now = _clock(now)
        observation = self.session.manager.join_observation
        if observation is not None:
            observation.decoded_batch(decoded.messages)
        for message in decoded.messages:
            if observation is not None:
                observation.receive(self.session, decoded.header, message)
            self.session.feed(decoded.header, message, tick=tick)
            if message.proto == PROTO_RELIABLE:
                if self.peer_setup is not None and self.session.update_sequence is None:
                    continue
                self.trade.feed_message(message, now=now)
            elif message.proto == 11 and message.port == 0 and message.protocol_specific == 0xFD:
                # Only the authenticated selected host and an installed Session
                # table may establish a separate broadcast receive stream.
                if self.session.update_sequence is None or decoded.header.src != self.session.host_var:
                    continue
                host_id = self.session.manager.host_mac.ljust(8, b'\0') + self.session.host_var.to_bytes(2, 'big')
                host = next((s for s in self.session.stations if s.identity == host_id), None)
                if host is None:
                    raise ClientProtocolError('broadcast_host_not_in_session')
                if self.broadcast is None:
                    self.broadcast = BroadcastReceiver(self.session.station_index, host.index)
                elif (self.broadcast.local_index, self.broadcast.host_index) != (self.session.station_index, host.index):
                    raise ClientProtocolError('broadcast_member_indices_changed')
                applications = self.broadcast.feed(message.payload)
                if self.peer_setup is not None or self.sync_save_peer is not None:
                    for application in applications:
                        self._receive_other_application(application.payload)
                    frame = parse_reliable(message.payload)
                    if not frame.is_application_data and frame.payload and self.broadcast_sender is not None:
                        self.broadcast_sender.feed_ack(message.payload)

    def drain(self, *, now: float, tick: int) -> tuple[OutboundPiaMessage, ...]:
        self.session.advance(tick)
        output = list(self.session.drain())
        if self.session.connected:
            if self.session.host_var is None:
                raise ClientProtocolError("connected Pia session has no host station id")
            if self.peer_setup is not None and self.broadcast is not None and self.broadcast.initialized:
                if self.broadcast_sender is None:
                    self.broadcast_sender = BroadcastSender(self.broadcast.local_index, self.broadcast.host_index, self.broadcast.capacity)
                elif self.broadcast_sender.capacity != self.broadcast.capacity:
                    raise ClientProtocolError('broadcast_capacity_changed')
                for protocol, payload in self.peer_setup.begin() + self.peer_setup.drain():
                    if protocol == 10:
                        self.trade.queue_peer_setup(payload, now=now)
                    else:
                        self.broadcast_sender.queue(payload)
                for payload in self.broadcast_sender.drain(now):
                    output.append(OutboundPiaMessage(protocol=11, payload=payload,
                        destination=self.session.host_var, source=self.session.our_var,
                        message_flags=DATA_MESSAGE_FLAGS, port=0, protocol_specific=0xFD))
            if self.broadcast is not None:
                ack = self.broadcast.drain_ack()
                if ack is not None:
                    output.append(OutboundPiaMessage(
                        protocol=11, payload=ack, destination=self.session.host_var,
                        source=self.session.our_var, message_flags=ACK_MESSAGE_FLAGS,
                        port=0, protocol_specific=0xFD))
            if self.sync_save_peer is not None:
                if (self.trade.complete and
                        (self.sync_start_guard is None or self.sync_start_guard())):
                    self.sync_save_peer.begin(now=now)
                for payload in self.sync_save_peer.drain(now=now):
                    self.trade.queue_sync_save(payload, now=now)
            for item in self.trade.drain(now=now):
                output.append(
                    OutboundPiaMessage(
                        protocol=PROTO_RELIABLE,
                        payload=item.payload,
                        destination=self.session.host_var,
                        source=self.session.our_var,
                        message_flags=item.message_flags,
                        port=item.port,
                        protocol_specific=item.protocol_specific,
                    )
                )
        return tuple(output)


@dataclass(frozen=True, slots=True)
class _OutboundTradeIdentity:
    """Stable identity of one complete Reliable title transaction.

    ``lowest_pending_ack`` is deliberately excluded: Reliable recomputes that
    sliding-window hint when it repacks a retransmission after an older send is
    acknowledged.  The sequence, application bytes, flags, and destination
    bitmap remain stable for the same transaction.
    """

    sequence_id: int
    flags: ReliableFlags
    application_payload: bytes
    destination_bit_count: int
    bitmap_words: tuple[int, ...]


def _outbound_trade_details(
    item: OutboundPiaMessage,
) -> tuple[TradeCommandID, _OutboundTradeIdentity] | None:
    """Return the command and stable identity carried by an outbound item."""

    if (
        getattr(item, "protocol", None) != PROTO_RELIABLE
        or getattr(item, "protocol_specific", None)
        != ZA_RELIABLE_PROTOCOL_SPECIFIC
    ):
        return None
    try:
        reliable = parse_reliable(item.payload)
        if not reliable.is_application_data:
            return None
        required = ReliableFlags.MESSAGE_START | ReliableFlags.MESSAGE_END
        if reliable.flags & required != required:
            return None
        frame = TradeFrame.from_wire_payload(reliable.payload)
    except (TypeError, ValueError):
        return None
    return (
        frame.command_id,
        _OutboundTradeIdentity(
            sequence_id=reliable.sequence_id,
            flags=reliable.flags,
            application_payload=reliable.payload,
            destination_bit_count=reliable.destination_bit_count,
            bitmap_words=reliable.bitmap_words,
        ),
    )


@dataclass(frozen=True, slots=True)
class LiveTradeResult:
    received_party_pa9: bytes
    elapsed: float
    output_path: Path | None
    completed: bool = True
    warning: str | None = None
    native_save_sync_complete: bool = False


@dataclass(frozen=True, slots=True)
class _OutputTarget:
    path: Path
    identity: tuple[int, int, int, int] | None
    content_identity: FileIdentity | None = None


def _target_identity(path: Path) -> tuple[int, int, int, int] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"trade output is not a regular file: {path}")
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _path_entry_identity(path: Path) -> str:
    """Canonicalize a pathname without following its final directory entry."""

    parent = path.parent.resolve(strict=False)
    return os.path.normcase(os.path.abspath(parent / path.name))


def _paths_alias(first: Path, second: Path) -> bool:
    if _path_entry_identity(first) == _path_entry_identity(second):
        return True
    try:
        return os.path.samefile(first, second)
    except FileNotFoundError:
        return False


def _preflight_trade_output(
    output: str | Path, *, overwrite: bool
) -> _OutputTarget:
    target = _absolute_path(output)
    if target.suffix.lower() not in {".pa9", ".epa9", ".ea9"}:
        raise ValueError("trade output must use .pa9, .epa9, or .ea9")
    if not target.parent.is_dir():
        raise ValueError(f"output directory does not exist: {target.parent}")
    identity = _target_identity(target)
    if identity is not None and not overwrite:
        raise FileExistsError(
            f"trade output already exists (pass overwrite=True/--force): {target}"
        )
    if identity is not None and identity[2] > 1024 * 1024:
        raise ValueError(f"refusing to replace unexpectedly large PA9 output: {target}")
    content_identity = file_identity(target)
    observed = None if content_identity is None else (
        content_identity.device, content_identity.inode,
        content_identity.size, content_identity.modified_ns,
    )
    if observed != identity:
        raise FileExistsError(f"trade output changed during preflight: {target}")
    if (os.name == "nt" and content_identity is not None
            and not stat.S_IMODE(content_identity.mode) & stat.S_IWRITE):
        raise PermissionError(f"PA9 output is read-only: {target}")

    # Verify that a same-directory atomic save is possible before any network
    # or irreversible title operation starts.
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.preflight-", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    Path(temporary_name).unlink()
    return _OutputTarget(target, identity, content_identity)


class _LivePacketWriter:
    def __init__(self, transport: DatagramTransport, crypto: PiaCrypto) -> None:
        self.transport = transport
        self.crypto = crypto
        # The nonce is also a big-endian replay-order value: main+0x241F600
        # reads it through 0x23AEFE0 and 0x242F7B0 rejects non-increasing
        # values for nonzero packet ids. Randomize the starting point only.
        # Reserve ample headroom and fail closed rather than wrap/reuse GCM IVs.
        self._header_nonce = 1 + (int.from_bytes(os.urandom(8), "big") >> 1)
        # ZA BoxTrade has one remote station.  Native Pia allocates transmit
        # packet ids by that remote StationAddress table slot, before outer
        # dst/protocol is selected.  Join, Finalize, RTT, Net updates, and
        # Reliable therefore share one sequence for this peer.
        self._next_peer_packet_id = 1

    def _next_packet_id(self, _destination: int) -> int:
        result = self._next_peer_packet_id
        self._next_peer_packet_id = 1 if result == 0xFFFF else result + 1
        return result

    def _new_header_nonce(self) -> bytes:
        if self._header_nonce > 0xFFFFFFFFFFFFFFFF:
            raise OverflowError("Pia header nonce exhausted; start a new session")
        nonce = self._header_nonce.to_bytes(8, "big")
        self._header_nonce += 1
        return nonce

    def encode(self, item: OutboundPiaMessage) -> tuple[bytes, str]:
        if item.packet_id is None:
            packet_id = self._next_packet_id(item.destination)
        else:
            packet_id = item.packet_id
            # Explicit diagnostic ids must not collide with the next normal
            # allocation. Connectionless Net id zero does not claim a slot.
            if packet_id == self._next_peer_packet_id:
                self._next_packet_id(item.destination)
        compact = build_message(
            item.protocol,
            item.payload,
            item.message_flags,
            port=item.port,
            protocol_specific=item.protocol_specific,
            # Native standalone Net/Session/RTT control messages use the
            # minimal 0x06 header (size + protocol).  Keep the forced-full
            # marker only for title messages that explicitly select framing
            # fields such as the Reliable stream id.
            force_full_header=any(
                value is not None
                for value in (
                    item.message_flags,
                    item.port,
                    item.protocol_specific,
                )
            ),
        )
        header = PiaHeader(
            dst=item.destination,
            src=item.source,
            packet_id=packet_id,
            nonce=self._new_header_nonce(),
            flags=(
                (PACKET_FLAG_SKIP_DESTINATION_CHECK if item.establishing else 0)
                | (PACKET_FLAG_SKIP_NETWORK_CHECK if item.skip_network_check else 0)
            ),
        )
        footer = b""
        if item.footer:
            footer_id = (
                item.footer_destination
                if item.footer_destination is not None
                else item.destination
            )
            footer = footer_id.to_bytes(2, "big")
        datagram = encode_datagram(
            compact,
            self.transport.our_ip,
            header,
            crypto=self.crypto,
            footer=footer,
            compress=item.compress,
        )
        destination_ip = (
            self.transport.host_ip if item.unicast else self.transport.broadcast
        )
        return datagram, destination_ip


def prepare_party_offer(
    value: str | Path | PA9 | bytes | bytearray | memoryview,
) -> bytes:
    """Validate and encode a complete 0x158 party PA9 for SelectPokemon.

    A 0x148 stored record is intentionally rejected.  Its party-stat tail must
    be calculated by the game/PKM model; padding sixteen zero bytes would make
    a correctly checksummed stored record into an invalid party member.
    """

    def reject_wrong_size(size: int) -> None:
        if size == PA9.STORED_SIZE:
            raise ValueError(
                "live trade requires a complete 0x158 party PA9; a 0x148 stored "
                "record cannot be safely widened by zero-padding party stats"
            )
        raise ValueError(
            f"live trade offer must contain exactly 0x{PA9.PARTY_SIZE:X} bytes "
            f"(got {size})"
        )

    def read_path(path: Path) -> bytes:
        initial_metadata = path.stat()
        if not stat.S_ISREG(initial_metadata.st_mode):
            raise ValueError(f"live trade offer is not a regular file: {path}")
        flags = os.O_RDONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        # Avoid blocking if a local path is swapped for a FIFO or device before
        # it is opened.  fstat below then rejects every non-regular descriptor.
        flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor: int | None = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"live trade offer is not a regular file: {path}")
            if metadata.st_size != PA9.PARTY_SIZE:
                reject_wrong_size(int(metadata.st_size))
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                data = stream.read(PA9.PARTY_SIZE + 1)
            if len(data) != PA9.PARTY_SIZE:
                reject_wrong_size(len(data))
            return data
        finally:
            if descriptor is not None:
                os.close(descriptor)

    source_raw: bytes | None = None
    if isinstance(value, PA9):
        record = value
    elif isinstance(value, (str, Path)):
        source_raw = read_path(Path(value).expanduser())
        record = PA9(source_raw)
    else:
        if len(value) != PA9.PARTY_SIZE:
            reject_wrong_size(len(value))
        source_raw = bytes(value)
        record = PA9(source_raw)
    if not record.is_party:
        reject_wrong_size(PA9.STORED_SIZE)
    if record.species_internal == 0:
        raise ValueError("live trade offer is an empty PA9 (species id is zero)")
    if source_raw is not None and not any(
        source_raw[PA9.STORED_SIZE : PA9.PARTY_SIZE]
    ):
        raise ValueError(
            "live trade offer has an all-zero party-stat tail; a stored PA9 "
            "must be converted by the game/PKM model, not zero-padded"
        )
    if not 1 <= record.stat_level <= 100 or not record.party_stats_present:
        raise ValueError(
            "live trade offer does not contain calculated party level/HP stats"
        )
    encoded = record.to_bytes(encrypted=True, party=True)
    if not any(encoded[PA9.STORED_SIZE : PA9.PARTY_SIZE]):
        raise ValueError(
            "live trade offer has an all-zero encrypted party tail; it appears "
            "to be an encrypted stored PA9 with padding appended"
        )
    return encoded


def _atomic_save_received_party(
    encrypted_party: bytes,
    output: str | Path,
    *,
    expected_target: _OutputTarget | None = None,
) -> Path:
    """Publish an approved PA9, sharing the editor's lock/no-clobber contract."""
    target = _absolute_path(output)
    if expected_target is None:
        expected_target = _preflight_trade_output(target, overwrite=False)
    if target != expected_target.path:
        raise ValueError("output target changed after preflight")
    with exclusive_save_lock(target):
        return _save_received_party_locked(encrypted_party, expected_target)


def _save_received_party_locked(
    encrypted_party: bytes, expected_target: _OutputTarget,
) -> Path:
    target = expected_target.path
    payload = _received_party_payload(encrypted_party, target)
    payload_digest = hashlib.sha256(payload).hexdigest()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    opened = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            opened = True
            if expected_target.content_identity is not None and hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), stat.S_IMODE(expected_target.content_identity.mode))
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if expected_target.content_identity is not None and not hasattr(os, "fchmod"):
            os.chmod(temporary, stat.S_IMODE(expected_target.content_identity.mode))
        if _target_identity(target) != expected_target.identity:
            raise FileExistsError(
                f"trade output changed after approval; refusing to overwrite: {target}"
            )
        publish_approved(
            temporary, target, expected_target.content_identity,
            read_identity=file_identity,
        )
        identity = file_identity(target)
        if (identity is None or identity.size != len(payload)
                or identity.sha256 != payload_digest):
            raise RuntimeError(f"received PA9 changed during publication: {target}")
        _sync_parent_directory(target.parent)
    except BaseException as operation_error:
        if not opened:
            try:
                os.close(descriptor)
            except OSError:
                pass
        cleanup_error = _cleanup_owned_temporary(temporary)
        if cleanup_error is not None and isinstance(operation_error, Exception):
            raise RuntimeError(
                f"{operation_error}; could not remove temporary PA9 data {temporary}"
            ) from operation_error
        raise
    else:
        cleanup_error = _cleanup_owned_temporary(temporary)
        if cleanup_error is not None:
            raise RuntimeError(f"received PA9 temporary cleanup failed: {temporary}") from cleanup_error
        _sync_parent_directory(target.parent)
        identity = file_identity(target)
        if (identity is None or identity.size != len(payload)
                or identity.sha256 != payload_digest):
            raise RuntimeError(f"received PA9 changed during publication: {target}")
    return target


def _received_party_payload(encrypted_party: bytes, target: Path) -> bytes:
    encrypted_output = target.suffix.lower() in {".ea9", ".epa9"}
    return PA9(encrypted_party, encrypted=True).to_bytes(
        encrypted=encrypted_output,
        party=True,
        update_checksum=True,
    )


def _sync_parent_directory(directory: Path) -> None:
    """Durably publish a create/replace where POSIX directory fsync exists."""
    _sync_output_directory(directory)


def _stage_committed_party(encrypted_party: bytes, target: _OutputTarget) -> Path:
    """Write and fsync a private recovery artifact before FinalAgreement."""

    payload = _received_party_payload(encrypted_party, target.path)
    descriptor, stage_name = tempfile.mkstemp(
        prefix=f".{target.path.stem}.pending-",
        suffix=target.path.suffix,
        dir=target.path.parent,
    )
    stage = Path(stage_name)
    opened = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            opened = True
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _sync_parent_directory(stage.parent)
    except BaseException:
        if not opened:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            stage.unlink()
        except FileNotFoundError:
            pass
        raise
    return stage


def _remove_staged_party(stage: Path) -> None:
    stage.unlink()
    _sync_parent_directory(stage.parent)


def _exclusive_recovery_save(encrypted_party: bytes, directory: Path, target: Path) -> Path:
    """Save committed data under a new name without replacing any path."""

    payload = _received_party_payload(encrypted_party, target)
    descriptor, recovery_name = tempfile.mkstemp(
        prefix=f"{target.stem}.recovered-",
        suffix=target.suffix,
        dir=directory,
    )
    recovery = Path(recovery_name)
    opened = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            opened = True
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _sync_parent_directory(directory)
    except BaseException:
        if not opened:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            recovery.unlink()
        except FileNotFoundError:
            pass
        raise
    return recovery


def _persist_committed_party(
    encrypted_party: bytes,
    target: _OutputTarget | None,
) -> tuple[Path | None, str | None]:
    """Persist an irreversible result, falling back to a collision-free file."""

    if target is None:
        return None, None
    try:
        return (
            _atomic_save_received_party(
                encrypted_party, target.path, expected_target=target
            ),
            None,
        )
    except BaseException as primary_error:
        directories = [target.path.parent]
        system_temporary = Path(tempfile.gettempdir())
        if system_temporary != target.path.parent:
            directories.append(system_temporary)
        recovery_errors: list[BaseException] = []
        for directory in directories:
            try:
                recovered = _exclusive_recovery_save(
                    encrypted_party, directory, target.path
                )
            except BaseException as exc:
                recovery_errors.append(exc)
                continue
            return (
                recovered,
                f"approved output could not be replaced ({type(primary_error).__name__}); "
                f"the received PA9 was preserved at {recovered}",
            )
        details = "; ".join(type(error).__name__ for error in recovery_errors)
        return (
            None,
            "the trade crossed FinalAgreement but the received PA9 could not be "
            f"written ({type(primary_error).__name__}; recovery: {details or 'unavailable'}); "
            "the returned LiveTradeResult still contains the encrypted party bytes",
        )


def run_live_trade(
    offer: str | Path | PA9 | bytes | bytearray | memoryview,
    *,
    output: str | Path | None = None,
    overwrite: bool = False,
    expected_output_identity: tuple[int, int, int, int] | None = None,
    auto_confirm: bool = False,
    confirmation_callback: Callable[[bytes], bool] | None = None,
    timeout: float = 120.0,
    settle_grace: float = 2.0,
    link_code: str | None = None,
    nickname: str = "ZA-Trade",
    keys_path: str | None = "~/.switch/prod.keys",
    relay: str | None = None,
    phyname: str = "phy0",
    ifname: str = "ldnclient",
    single_interface: bool | None = None,
    bss_helper: str | None = None,
    transport_factory: Callable[[], DatagramTransport] | None = None,
    identification_token: bytes | bytearray | memoryview | None = None,
    title_version: str = DEFAULT_ZA_TITLE_VERSION,
    peer_user_data: UserData | None = None,
    sync_save: bool = False,
    log=print,
    capture: Callable[[str, bytes], None] | None = None,
    cancel_event: CancellationToken | None = None,
) -> LiveTradeResult:
    """Run one experimental local trade through a local or relayed Wi-Fi PHY.

    Direct mode requires Linux/root, ``ldn`` dependencies, and the caller's
    own production keys.  Windows can set ``relay`` and keep the keys solely on
    the Linux/WSL2 relay.  ``auto_confirm`` defaults to false so merely calling
    this function cannot approve an exchange; callers performing an unattended
    exchange must explicitly pass ``auto_confirm=True``.  A caller that wants
    an interactive policy can instead provide
    ``confirmation_callback``; it is called once with the remote encrypted
    party PA9 and must return true to approve.  After both FinalAgreement
    commands, network pumping continues through ``settle_grace`` and until the
    local Reliable send window is acknowledged.  An externally obtained Pia
    identification token may be injected byte-exactly; otherwise
    ``title_version`` selects the public compatibility tag (default 2.0.2).
    """

    # Full-game save synchronization is separate from the legacy negotiation-only
    # proof. It requires the native peer dataset and a real durable PC output.
    if type(sync_save) is not bool:
        raise TypeError('sync_save must be bool')
    if sync_save and (peer_user_data is None or output is None):
        raise ValueError('sync_save requires native peer data and a durable output')
    # Validate and freeze the token before file access or transport construction.
    if peer_user_data is not None and not isinstance(peer_user_data, UserData):
        raise TypeError('peer_user_data must be source-validated UserData or None')
    identification_token = identification_token_for_title(title_version, identification_token)
    timeout = _clock(timeout)
    if timeout == 0:
        raise ValueError("timeout must be positive")
    settle_grace = _clock(settle_grace)
    if not isinstance(auto_confirm, bool):
        raise TypeError("auto_confirm must be a bool")
    if not isinstance(overwrite, bool):
        raise TypeError("overwrite must be a bool")
    if expected_output_identity is not None:
        if (
            not isinstance(expected_output_identity, tuple)
            or len(expected_output_identity) != 4
            or any(
                not isinstance(item, int) or isinstance(item, bool)
                for item in expected_output_identity
            )
        ):
            raise TypeError("expected_output_identity must be a four-integer tuple")
        if not overwrite:
            raise ValueError("expected_output_identity requires overwrite=True")
        if output is None:
            raise ValueError("expected_output_identity requires an output path")
    if single_interface is not None and not isinstance(single_interface, bool):
        raise TypeError("single_interface must be a bool or None")
    if confirmation_callback is not None and not callable(confirmation_callback):
        raise TypeError("confirmation_callback must be callable or None")
    validate_cancel_event(cancel_event)
    if not auto_confirm and confirmation_callback is None:
        raise ValueError(
            "live trade needs confirmation_callback or explicit auto_confirm=True"
        )
    if bss_helper is not None and (relay is not None or transport_factory is not None):
        raise ValueError("BSS helper must be configured on the local Linux radio owner, not a relay client or injected transport")
    if transport_factory is not None and relay is not None:
        raise ValueError("transport_factory and relay are mutually exclusive")
    room_code = (
        validate_link_code(link_code)
        if link_code is not None
        else None
    )
    if room_code is None and transport_factory is None:
        raise ValueError("live Z-A trade requires an 8-digit Link Code")
    raise_if_cancelled(cancel_event)
    offer_path = Path(offer).expanduser() if isinstance(offer, (str, Path)) else None
    output_path = Path(output).expanduser() if output is not None else None
    if (
        offer_path is not None
        and output_path is not None
        and _paths_alias(offer_path, output_path)
    ):
        raise ValueError("offer and output must be different files")
    output_target = (
        _preflight_trade_output(output_path, overwrite=overwrite)
        if output_path is not None
        else None
    )
    if (
        expected_output_identity is not None
        and output_target is not None
        and output_target.identity != expected_output_identity
    ):
        raise FileExistsError(
            f"trade output changed after approval; refusing to overwrite: "
            f"{output_target.path}"
        )
    party = prepare_party_offer(offer)
    transport: DatagramTransport
    if transport_factory is not None:
        transport = transport_factory()
    elif relay is not None:
        transport = RelayTransport(
            relay,
            link_code=room_code,
            nickname=nickname,
            communication_id=ZA_PROFILE.title_id_int,
            scene_id=1,
            expected_max_participants=2,
            log=log,
        )
    else:
        if os.name == "nt":
            raise RuntimeError(
                "direct LDN requires Linux; on Windows start relay-serve in "
                "WSL2/Linux and pass --relay HOST:PORT"
            )
        if not keys_path:
            raise ValueError("direct Linux LDN mode requires keys_path")
        transport = make_local_transport(
            LiveTransport, link_code=room_code,
            nickname=nickname, keys_path=keys_path,
            phyname=phyname, ifname=ifname, log=log,
            single_interface=single_interface,
            **({"bss_helper": bss_helper} if bss_helper is not None else {}),
        )
    committed_offer: bytes | None = None
    committed_final_identity: _OutboundTradeIdentity | None = None
    last_committed_result: LiveTradeResult | None = None
    pending_commit_path: Path | None = None
    committed_saved_path: Path | None = None
    committed_save_warning: str | None = None
    committed_persistence_attempted = False
    join_observation = JoinObservation()
    session = None

    def finish_transport() -> LiveTradeResult | None:
        """Stop once; override an outcome only if cleanup fails with a receipt."""

        try:
            try:
                transport.stop()
            except BaseException as cleanup_error:
                if last_committed_result is None:
                    raise
                cleanup_warning = (
                    "radio/relay cleanup failed after FinalAgreement "
                    f"({type(cleanup_error).__name__}); do not retry the trade until "
                    "the adapter and both game screens have been checked"
                )
                try:
                    log(f"[trade] WARNING: {cleanup_warning}: {cleanup_error}")
                except BaseException:
                    pass
                combined_warning = "; ".join(
                    part
                    for part in (last_committed_result.warning, cleanup_warning)
                    if part
                )
                # A cleanup failure must not discard the already persisted,
                # irreversible outcome or turn it into a generic exception that
                # invites a retry.  Mark the result indeterminate and surface the
                # cleanup warning to both CLI and GUI.
                return replace(
                    last_committed_result,
                    completed=False,
                    warning=combined_warning,
                )
        finally:
            join_observation.emit(log, session)
            if committed_offer is None and pending_commit_path is not None:
                try:
                    _remove_staged_party(pending_commit_path)
                except BaseException:
                    pass
        return None

    try:
        start_transport(transport, cancel_event)
        raise_if_cancelled(cancel_event)
        if room_code is not None and not advertised_link_code_matches(
            transport.app_data, room_code
        ):
            raise ClientProtocolError(
                "joined LDN room does not advertise the requested Link Code"
            )
        # The trade timeout covers Pia/trade activity, not a potentially slow
        # radio scan and LDN association performed by start().
        started = time.monotonic()
        session = ZASession(
            application_data=transport.app_data,
            our_mac=transport.our_mac,
            host_mac=transport.host_mac,
            our_ip=transport.our_ip,
            host_ip=transport.host_ip,
            player_name=nickname,
            identification_token=identification_token,
            title_version=title_version,
            log=log,
            join_observation=join_observation,
        )
        # Keep the wire engine policy-neutral here.  The live adapter validates
        # the received PA9 before either interactive or unattended approval.
        trade = ReliableTradeEngine(offer=party, auto_confirm=False)
        client = (ZATradeClient(session, trade) if peer_user_data is None else
                  ZATradeClient(session, trade, peer_user_data=peer_user_data))
        crypto = PiaCrypto(transport.ssid, profile=ZA_PROFILE)
        writer = _LivePacketWriter(transport, crypto)
        confirmation_offer: bytes | None = None
        confirmation_approved = False
        seen_remote_cancels = trade.negotiation.remote_cancel_count
        confirmation_declined_at: float | None = None
        completed_at: float | None = None
        postcommit_terminal_at: float | None = None
        postcommit_terminal_reason: str | None = None
        confirmation_generation = 0
        confirmation_worker: threading.Thread | None = None
        confirmation_worker_generation: int | None = None
        confirmation_results: queue.SimpleQueue[
            tuple[int, bytes, object | None, BaseException | None]
        ] = queue.SimpleQueue()

        def log_before_commit(message: str) -> None:
            """A routine logger failure must not decide whether a trade is sent."""

            try:
                log(message)
            except Exception:
                pass

        def log_after_commit(message: str) -> None:
            """Never let diagnostics discard an irreversible trade result."""

            try:
                log(message)
            except BaseException:
                pass

        def latch_postcommit_stop(now: float) -> None:
            """Remember a stop observed after the irreversible send boundary."""

            nonlocal postcommit_terminal_at, postcommit_terminal_reason
            if (
                committed_offer is None
                or postcommit_terminal_at is not None
                or cancel_event is None
                or not cancel_event.is_set()
            ):
                return
            postcommit_terminal_at = now
            postcommit_terminal_reason = (
                "stop was requested after local FinalAgreement; the console "
                "outcome is indeterminate"
            )
            log_after_commit(
                "[trade] stop requested after FinalAgreement; preserving "
                "the received PA9 while finishing a bounded network grace"
            )

        def request_confirmation(generation: int, remote_offer: bytes) -> None:
            try:
                assert confirmation_callback is not None
                decision: object = confirmation_callback(remote_offer)
            except BaseException as exc:
                confirmation_results.put((generation, remote_offer, None, exc))
            else:
                confirmation_results.put((generation, remote_offer, decision, None))

        def persist_committed_now() -> None:
            nonlocal pending_commit_path
            nonlocal committed_saved_path, committed_save_warning
            nonlocal committed_persistence_attempted
            if committed_persistence_attempted:
                return
            committed_persistence_attempted = True
            assert committed_offer is not None
            try:
                saved, save_warning = _persist_committed_party(
                    committed_offer, output_target
                )
            except BaseException as exc:  # final fail-safe around recovery code
                saved = None
                save_warning = (
                    "the committed PA9 persistence path failed unexpectedly "
                    f"({type(exc).__name__})"
                )

            if saved is None and pending_commit_path is not None:
                saved = pending_commit_path
                pending_commit_path = None
                pending_warning = (
                    "the fsynced write-ahead PA9 was preserved at "
                    f"{saved}; inspect it before retrying"
                )
                save_warning = "; ".join(
                    part for part in (save_warning, pending_warning) if part
                )
            elif pending_commit_path is not None:
                stage = pending_commit_path
                try:
                    _remove_staged_party(stage)
                except BaseException as exc:
                    pending_warning = (
                        "the committed PA9 was saved, but its write-ahead copy "
                        f"may remain at {stage} ({type(exc).__name__})"
                    )
                    save_warning = "; ".join(
                        part for part in (save_warning, pending_warning) if part
                    )
                else:
                    pending_commit_path = None
            committed_saved_path = saved
            committed_save_warning = save_warning

        def committed_result(
            elapsed: float,
            *,
            completed: bool,
            warning: str | None = None,
        ) -> LiveTradeResult:
            nonlocal last_committed_result
            assert committed_offer is not None
            persist_committed_now()
            combined_warning = "; ".join(
                part for part in (warning, committed_save_warning) if part
            ) or None
            last_committed_result = LiveTradeResult(
                committed_offer,
                elapsed,
                committed_saved_path,
                completed=completed,
                warning=combined_warning,
                native_save_sync_complete=bool(sync_peer is not None and sync_peer.complete),
            )
            if combined_warning is not None:
                log_after_commit(f"[trade] WARNING: {combined_warning}")
            return last_committed_result

        sync_peer = None
        if sync_save:
            def verify_sync_artifact(path):
                if path is None or committed_offer is None or output_target is None:
                    return False
                expected = _received_party_payload(committed_offer, output_target.path)
                # Windows FlushFileBuffers needs write access. These are only
                # this transaction's own staged/published received files.
                access = os.O_RDWR if os.name == 'nt' else os.O_RDONLY
                fd = os.open(path, access | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(fd, 'rb') as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        return False
                    if stream.read(len(expected)+1) != expected:
                        return False
                    os.fsync(stream.fileno())
                _sync_parent_directory(Path(path).parent)
                return True

            def prepare_sync():
                return (confirmation_approved and committed_offer is not None
                        and committed_offer == confirmation_offer
                        and (verify_sync_artifact(pending_commit_path)
                             if pending_commit_path is not None
                             else verify_sync_receipt()))

            def persist_sync():
                persist_committed_now()
                return (committed_save_warning is None and output_target is not None
                        and committed_saved_path == output_target.path
                        and verify_sync_artifact(committed_saved_path))

            def verify_sync_receipt():
                return (committed_save_warning is None and output_target is not None
                        and committed_saved_path == output_target.path
                        and verify_sync_artifact(committed_saved_path))

            sync_peer = SyncSavePeer(prepare=prepare_sync, persist=persist_sync,
                                     commit=verify_sync_receipt, finish=verify_sync_receipt)
            client.sync_save_peer = sync_peer
            # Negotiation can queue Final before the live writer has staged it.
            # Begin save sync only after the real send boundary was crossed.
            client.sync_start_guard = lambda: committed_offer is not None

        def exchange_complete():
            return trade.complete and (sync_peer is None or sync_peer.complete)

        try:
            while True:
                now = time.monotonic() - started
                cancelled_now = bool(
                    cancel_event is not None and cancel_event.is_set()
                )
                if cancelled_now and committed_offer is None:
                    raise_if_cancelled(cancel_event)
                if cancelled_now:
                    latch_postcommit_stop(now)

                if now >= timeout:
                    if committed_offer is not None:
                        fully_settled = bool(
                            postcommit_terminal_at is None
                            and exchange_complete()
                            and completed_at is not None
                            and now - completed_at >= settle_grace
                            and not trade.unacknowledged
                        )
                        committed_result(
                            now,
                            completed=fully_settled,
                            warning=(
                                None
                                if fully_settled
                                else postcommit_terminal_reason
                                or (
                                    "trade timeout occurred after local FinalAgreement; "
                                    "the console outcome is indeterminate"
                                    if not exchange_complete()
                                    else "trade completed, but post-agreement "
                                    "acknowledgement or settle grace did not finish "
                                    "before the timeout"
                                )
                            ),
                        )
                        break
                    if confirmation_declined_at is not None:
                        raise TradeDeclined(
                            "the received Z-A trade offer was declined; Cancel was "
                            "not fully acknowledged before the trade timeout"
                        )
                    if completed_at is not None:
                        detail = " (waiting for post-agreement grace/acknowledgement)"
                    elif trade.remote_offer is not None and not confirmation_approved:
                        detail = " (remote offer received but not approved)"
                    else:
                        detail = ""
                    raise TimeoutError(
                        f"Z-A local trade timed out after {timeout:.1f}s{detail}"
                    )

                tick = int(now * 60)
                for datagram, source_ip in transport.recv():
                    join_observation.udp_host += source_ip == transport.host_ip
                    join_observation.udp_other += source_ip != transport.host_ip
                    if capture is not None:
                        capture("in", bytes(datagram))
                    join_observation.decode_started += 1
                    decoded = decode_datagram(
                        datagram, source_ip, profile=ZA_PROFILE, crypto=crypto
                    )
                    join_observation.decoded += 1
                    client.feed_decoded(decoded, now=now, tick=tick)

                if trade.negotiation.remote_cancel_count != seen_remote_cancels:
                    seen_remote_cancels = trade.negotiation.remote_cancel_count
                    confirmation_generation += 1
                    confirmation_offer = None
                    confirmation_approved = False
                    if committed_offer is not None and postcommit_terminal_at is None:
                        postcommit_terminal_at = now
                        postcommit_terminal_reason = (
                            "the remote peer cancelled after local FinalAgreement; "
                            "the outcome is indeterminate and no replacement offer "
                            "was accepted"
                        )
                if (
                    confirmation_approved
                    and confirmation_offer is not None
                    and trade.remote_offer != confirmation_offer
                ):
                    raise ClientProtocolError(
                        "remote peer changed its committed PA9 after local confirmation"
                    )

                while True:
                    try:
                        generation, offered, decision, error = (
                            confirmation_results.get_nowait()
                        )
                    except queue.Empty:
                        break
                    if generation == confirmation_worker_generation:
                        confirmation_worker = None
                        confirmation_worker_generation = None
                    if (
                        generation != confirmation_generation
                        or offered != confirmation_offer
                        or trade.remote_offer != offered
                    ):
                        continue
                    if error is not None:
                        raise error
                    if not isinstance(decision, bool):
                        raise TypeError("confirmation_callback must return a bool")
                    if decision:
                        raise_if_cancelled(cancel_event)
                        confirmation_approved = True
                        trade.confirm(now=now)
                    else:
                        # Reason 0 is the title's neutral cancel/reselection path.
                        # Reasons 1 and 2 report a legality problem with one side's
                        # Pokemon and must not be fabricated for a user decline.
                        trade.set_offer(None)
                        trade.cancel(now=now)
                        confirmation_declined_at = now

                if (
                    postcommit_terminal_at is None
                    and
                    trade.remote_offer is not None
                    and trade.remote_offer != confirmation_offer
                    and trade.negotiation.local_state is TradeState.OFFERED
                    # Reliable may deliver the peer's Select and Confirm in
                    # the same receive batch. Do not strand a validated offer
                    # merely because the peer already reached CONFIRMED.
                    and trade.negotiation.remote_state in (
                        TradeState.OFFERED, TradeState.CONFIRMED
                    )
                    and (
                        auto_confirm
                        or confirmation_worker is None
                        or not confirmation_worker.is_alive()
                    )
                ):
                    confirmation_offer = bytes(trade.remote_offer)
                    confirmation_generation += 1
                    # Structural validation is mandatory even for --yes.
                    if not any(
                        trade.remote_offer[PA9.STORED_SIZE : PA9.PARTY_SIZE]
                    ):
                        raise ClientProtocolError(
                            "remote SelectPokemon has an all-zero encrypted party tail"
                        )
                    remote_record = PA9(trade.remote_offer, encrypted=True)
                    if remote_record.species_internal == 0:
                        raise ClientProtocolError(
                            "remote SelectPokemon contains an empty PA9"
                        )
                    if (
                        not 1 <= remote_record.stat_level <= 100
                        or not remote_record.party_stats_present
                    ):
                        raise ClientProtocolError(
                            "remote SelectPokemon lacks calculated party level/HP stats"
                        )
                    if auto_confirm:
                        confirmation_results.put(
                            (
                                confirmation_generation,
                                confirmation_offer,
                                True,
                                None,
                            )
                        )
                    else:
                        if confirmation_callback is None:  # defensive invariant
                            raise AssertionError("confirmation policy disappeared")
                        confirmation_worker = threading.Thread(
                            target=request_confirmation,
                            args=(confirmation_generation, confirmation_offer),
                            name="za-trade-confirmation",
                            daemon=True,
                        )
                        confirmation_worker_generation = confirmation_generation
                        confirmation_worker.start()

                if sync_peer is not None and postcommit_terminal_at is not None:
                    sync_peer.suspend()
                outbound = client.drain(now=now, tick=tick)
                for item in outbound:
                    if (sync_peer is not None and postcommit_terminal_at is not None
                            and item.protocol == PROTO_RELIABLE):
                        sync_frame = parse_reliable(item.payload)
                        if sync_frame.is_application_data and sync_frame.payload[:1] == b'\x02':
                            continue
                    if committed_offer is None:
                        raise_if_cancelled(cancel_event)
                    is_join = item.protocol == 13 and item.payload[:1] == b"\x00"
                    if is_join:
                        join_observation.mark("encode_started")
                    datagram, destination_ip = writer.encode(item)
                    if is_join:
                        join_observation.mark("encoded")
                    if capture is not None:
                        capture("out", datagram)
                    trade_details = _outbound_trade_details(item)
                    command_id = (
                        trade_details[0] if trade_details is not None else None
                    )
                    trade_identity = (
                        trade_details[1] if trade_details is not None else None
                    )
                    if committed_offer is not None and command_id is not None:
                        if (
                            postcommit_terminal_at is not None
                            or committed_final_identity is None
                            or trade_identity != committed_final_identity
                        ):
                            # Never start a second title transaction after the
                            # first irreversible attempt.  During an ordinary
                            # settle only the same Reliable FinalAgreement
                            # transaction may be retransmitted.  Its mutable
                            # lowest-pending-ACK field is not part of identity.
                            continue
                    if (
                        committed_offer is None
                        and command_id is TradeCommandID.FINAL_AGREEMENT
                    ):
                        # The state machine may queue FinalAgreement before this
                        # loop runs.  Cancellation still wins until the actual
                        # network send attempt.  Once that attempt begins, a
                        # raised/ambiguous send must be treated conservatively as
                        # irreversible because the datagram may have left the host.
                        raise_if_cancelled(cancel_event)
                        if not confirmation_approved or confirmation_offer is None:
                            raise ClientProtocolError(
                                "local FinalAgreement was queued without an approved offer"
                            )
                        approved_offer = bytes(confirmation_offer)
                        if output_target is not None:
                            pending_commit_path = _stage_committed_party(
                                approved_offer, output_target
                            )
                        # Staging and diagnostics may block.  Cancellation wins
                        # until no fallible callback remains before send.
                        raise_if_cancelled(cancel_event)
                        log_before_commit(
                            "[trade] sending local FinalAgreement; subsequent "
                            "stop/timeout will preserve the received PA9"
                        )
                        raise_if_cancelled(cancel_event)
                        # This is the irreversible marker: keep it immediately
                        # adjacent to the network send attempt.  Any exception
                        # from send is conservatively treated as ambiguous.
                        assert trade_identity is not None
                        committed_final_identity = trade_identity
                        committed_offer = approved_offer
                        try:
                            transport.send(datagram, destination_ip)
                        finally:
                            # Publish immediately after the send attempt.  If
                            # the process dies inside send, the fsynced stage is
                            # still available for recovery.
                            persist_committed_now()
                    else:
                        if is_join:
                            join_observation.mark("send_started")
                        transport.send(datagram, destination_ip)
                        if is_join:
                            join_observation.mark("send_returned")

                # A send callback or another thread can request stop after the
                # loop-entry snapshot.  Re-sample at the completion boundary so
                # that request cannot be mistaken for a fully settled exchange.
                if (
                    committed_offer is not None
                    and cancel_event is not None
                    and cancel_event.is_set()
                ):
                    latch_postcommit_stop(time.monotonic() - started)

                if (
                    postcommit_terminal_at is not None
                    and committed_offer is not None
                    and now - postcommit_terminal_at >= settle_grace
                ):
                    committed_result(
                        now,
                        completed=False,
                        warning=postcommit_terminal_reason,
                    )
                    break

                if confirmation_declined_at is not None:
                    decline_settled = now - confirmation_declined_at >= settle_grace
                    if decline_settled or not trade.unacknowledged:
                        raise TradeDeclined(
                            "the received Z-A trade offer was declined"
                        )

                if (
                    postcommit_terminal_at is None
                    and exchange_complete()
                    and confirmation_offer is not None
                ):
                    if committed_offer is None:
                        raise ClientProtocolError(
                            "trade engine reported completion before local FinalAgreement send"
                        )
                    if completed_at is None:
                        completed_at = now
                    settled = now - completed_at >= settle_grace
                    if settled and not trade.unacknowledged:
                        committed_result(now, completed=True)
                        break

                time.sleep(1 / 120)
        except BaseException as exc:
            if committed_offer is not None:
                committed_result(
                    time.monotonic() - started,
                    completed=False,
                    warning=(
                        f"{type(exc).__name__} occurred after local FinalAgreement; "
                        "the console outcome may be indeterminate"
                    ),
                )
            else:
                raise
    except BaseException:
        # Preserve the former finally precedence: successful cleanup re-raises
        # the active error; failed cleanup may replace it only with a receipt
        # that has already been recorded.  A failure in finish_transport itself
        # propagates normally, without a second stop attempt.
        cleanup_result = finish_transport()
        if cleanup_result is None:
            raise
        return cleanup_result

    cleanup_result = finish_transport()
    if cleanup_result is not None:
        return cleanup_result
    assert last_committed_result is not None
    return last_committed_result


__all__ = [
    "ACK_MESSAGE_FLAGS",
    "BULK_ACK_FLAGS",
    "ClientProtocolError",
    "DATA_MESSAGE_FLAGS",
    "DATA_FLAGS",
    "INITIAL_DATA_FLAGS",
    "INITIAL_RELIABLE_SEQUENCE",
    "MAX_RECEIVED_COMMAND_HISTORY",
    "LiveTradeResult",
    "OperationCancelled",
    "PROTO_RELIABLE",
    "ReliableOutbound",
    "ReliableTimeout",
    "ReliableTradeEngine",
    "TradeDeclined",
    "ZA_RELIABLE_PORT",
    "ZA_RELIABLE_PROTOCOL_SPECIFIC",
    "ZATradeClient",
    "prepare_party_offer",
    "run_live_trade",
]
