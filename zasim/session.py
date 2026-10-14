"""Pokemon Legends: Z-A Pia session adapter.

The Net/RTT/Session handshake lives in :mod:`zasim.ldn.pia_connect`.
This module supplies Z-A's recovered protocol-version table and, importantly,
uses the application communication version advertised by the LDN host.  It
requires the host's advertised version instead of a title-specific fallback.

The live path is hardware-unverified.  All parsing and queued-message behavior
is deterministic and can be exercised without a console or production keys.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from threading import Lock
from typing import Callable, Iterable

from zasim.ldn.pia_connect import (
    ConnectionManager,
    PROTO_NET,
    PROTO_SESSION,
    NET_CONN_REQUEST,
    SESSION_VAR,
    ldn_constant_id,
    parse_net,
)

from .compatibility import (
    DEFAULT_ZA_TITLE_VERSION, ZA_IDENTIFICATION_TOKEN as ZA_IDENTIFICATION_TOKEN,
    identification_token_for_title,
)
from .pia import PiaHeader, PiaMessage
from .session_update import UpdateAssembler, parse_update


# 2.0.2 running-game registry, observed without guest writes or code patches.
# A six-entry subset fails the native unique-protocol-count
# check (main+0x254a08c..94 -> silent return path).  This is the complete Pia
# library fingerprint, not a claim that every protocol is used by BoxTrade.
ZA_PROTOCOL_VERSIONS: tuple[tuple[int, int], ...] = (
    (1, 0),
    (3, 5),
    (5, 1),
    (6, 0),
    (9, 1),
    (10, 3),
    (11, 4),
    (12, 4),
    (13, 7),
    (15, 0),
)
LDN_APPLICATION_VERSION_OFFSET = 3
LDN_APPLICATION_VERSION_END = 5
LDN_SYSTEM_PROPERTY_SIZE = 0x5C
LDN_SYSTEM_COMMUNICATION_VERSION = 22
MIN_VARIABLE_ID = 2
MAX_VARIABLE_ID = 0xFFFF
# 0x23bb8d4..e0: high32(random32 * 0xfffd) + 2 -> [2, 0xfffe].
# Explicit injected IDs remain wire-valid through 0xffff; this is RNG policy.
MAX_GENERATED_VARIABLE_ID = 0xFFFE
_variable_id_lock = Lock()
_last_generated_variable_id: int | None = None


class AdvertisementError(ValueError):
    """The host's LDN advertisement lacks required Pia session data."""


def _new_variable_id(*, exclude: Iterable[int] = ()) -> int:
    """Generate in the native range, excluding peers and the previous draw.

    0x23c19e4..1a08 excludes previous per-Net and process-wide IDs. The
    rotation caller excludes its current (colliding) ID as a peer; serialize
    the process-wide last-ID check too. Entropy is not the native tick PRNG.
    """

    global _last_generated_variable_id
    blocked = {int(value) for value in exclude}
    span = MAX_GENERATED_VARIABLE_ID - MIN_VARIABLE_ID + 1
    with _variable_id_lock:
        blocked.add(_last_generated_variable_id)
        if sum(MIN_VARIABLE_ID <= value <= MAX_GENERATED_VARIABLE_ID
               for value in blocked if value is not None) == span:
            raise ValueError("no available generated variable ids")
        while True:
            candidate = MIN_VARIABLE_ID + ((secrets.randbits(32) * span) >> 32)
            if candidate not in blocked:
                _last_generated_variable_id = candidate
                return candidate


def application_communication_version(
    application_data: bytes | bytearray | memoryview,
) -> int:
    """Read the generic Pia application communication version (BE16).

    Pia 6.16--6.41 places this field at bytes 3..5 of LDN application data.
    A missing header is an error: silently substituting another title's value
    produces a well-formed but incompatible Session join.
    """

    try:
        data = bytes(application_data)
    except (TypeError, ValueError) as exc:
        raise TypeError("application_data must be bytes-like") from exc
    if len(data) < LDN_SYSTEM_PROPERTY_SIZE:
        raise AdvertisementError(
            "LDN application data is shorter than the Pia system-property "
            f"header (need 0x5C bytes, got {len(data)})"
        )
    declared_size = int.from_bytes(data[0:2], "big")
    if declared_size != LDN_SYSTEM_PROPERTY_SIZE:
        raise AdvertisementError(
            "unexpected Pia system-property size "
            f"0x{declared_size:04X} (expected 0x005C)"
        )
    if data[2] != LDN_SYSTEM_COMMUNICATION_VERSION:
        raise AdvertisementError(
            "unexpected Pia system communication version "
            f"{data[2]} (expected {LDN_SYSTEM_COMMUNICATION_VERSION})"
        )
    return int.from_bytes(
        data[LDN_APPLICATION_VERSION_OFFSET:LDN_APPLICATION_VERSION_END], "big"
    )


@dataclass(frozen=True, slots=True)
class OutboundPiaMessage:
    """A compact-message payload plus Pia packet routing requirements."""

    protocol: int
    payload: bytes
    destination: int
    source: int
    compress: bool = False
    footer: bool = True
    establishing: bool = False
    unicast: bool = True
    packet_id: int | None = None
    footer_destination: int | None = None
    message_flags: int | None = None
    port: int | None = None
    protocol_specific: int | None = None
    skip_network_check: bool = False


class ZASession:
    """Thin Z-A configuration wrapper around the generic Pia handshake."""

    def __init__(
        self,
        *,
        application_data: bytes | bytearray | memoryview,
        our_mac: bytes,
        host_mac: bytes,
        our_ip: str,
        host_ip: str,
        our_var: int | None = None,
        player_name: str = "ZA-Trade",
        random4: bytes | bytearray | memoryview | None = None,
        log=lambda *args: None,
        protocols: Iterable[tuple[int, int]] = ZA_PROTOCOL_VERSIONS,
        player_id: bytes | bytearray | memoryview | None = None,
        identification_token: bytes | bytearray | memoryview | None = None,
        clock: Callable[[], float] | None = None,
        random32: Callable[[], int] | None = None,
        join_observation=None,
        title_version: str = DEFAULT_ZA_TITLE_VERSION,
    ) -> None:
        self.identification_token = identification_token_for_title(
            title_version, identification_token
        )
        self.title_version = title_version
        version = application_communication_version(application_data)
        self.application_version = version
        self._generated_our_var = our_var is None
        if our_var is None:
            our_var = _new_variable_id()
        elif (
            not isinstance(our_var, int)
            or isinstance(our_var, bool)
            or not MIN_VARIABLE_ID <= our_var <= MAX_VARIABLE_ID
        ):
            raise ValueError("our_var must be an integer from 2 through 65535")
        if not isinstance(player_name, str):
            raise TypeError("player_name must be a string")
        if random4 is not None:
            random4 = bytes(random4)
            if len(random4) != 4:
                raise ValueError("random4 must contain exactly 4 bytes")
        if player_id is None:
            generated = bytes(16)
            while not any(generated):
                generated = os.urandom(16)
            player_id = generated
        self.player_id = bytes(player_id)
        if len(self.player_id) != 16:
            raise ValueError("player_id must contain exactly 16 bytes")
        self.station_index: int | None = None
        self.station_generation: int | None = None
        self.assignment_sequence: int | None = None
        self.update_sequence: int | None = None
        self._update_generation = 0
        self.stations = ()
        self._updates = UpdateAssembler()
        self.manager = ConnectionManager(
            ldn_constant_id(our_mac),
            ldn_constant_id(host_mac),
            our_ip,
            host_ip,
            our_var=our_var,
            player_name=player_name,
            random4=random4,
            log=log,
            app_ver=version.to_bytes(2, "big"),
            protocols=tuple(protocols),
            player_id=self.player_id,
            identification_token=self.identification_token,
            clock=clock,
            random32=random32,
            join_observation=join_observation,
        )

    @property
    def random4(self) -> bytes:
        """Most recently built Join random4; zero before an unseeded first send."""
        return self.manager.random4

    @property
    def connected(self) -> bool:
        return self.manager.connected

    @property
    def our_var(self) -> int:
        return self.manager.our_var

    @property
    def host_var(self) -> int | None:
        return self.manager.host_var

    def feed(
        self, header: PiaHeader, message: PiaMessage, *, tick: int | None = None,
        now: float | None = None
    ) -> None:
        """Feed one authenticated compact message into Net/RTT/Session."""

        if not isinstance(header, PiaHeader):
            raise TypeError("header must be a PiaHeader")
        if not isinstance(message, PiaMessage):
            raise TypeError("message must be a PiaMessage")
        self.manager.raise_if_session_failed()
        # Reject unrelated destinations before collision handling can rotate
        # our id. Invalid input must not leave a partially changed session.
        if header.dst not in (0, SESSION_VAR, self.manager.our_var):
            raise ValueError(
                "incoming Pia destination variable id does not match our session id"
            )

        incoming_host_ids = {
            value
            for value in (header.src,)
            if MIN_VARIABLE_ID <= value <= MAX_VARIABLE_ID
        }
        if message.proto == PROTO_NET:
            net = parse_net(message.payload)
            if net is None or net[0] != 1:
                return
            if net[1] == NET_CONN_REQUEST:
                # Validation is pure: no learned ids, outbox entries, cached
                # payloads or retry deadlines change for rejected statuses.
                status = self.manager.validate_net_status(message.payload)
                if status is None:
                    return
                incoming_host_ids.add(status[0])

        if len(incoming_host_ids) > 1:
            raise ValueError("Pia header and Net status host variable ids disagree")
        if (self.manager.host_var is not None
                and any(value != self.manager.host_var for value in incoming_host_ids)):
            raise ValueError("incoming Pia host variable id changed within the session")

        our_var = self.manager.our_var
        if our_var in incoming_host_ids:
            if not self._generated_our_var:
                raise ValueError("explicit our_var collides with the host variable id")
            if self.manager.local_identity_announced:
                raise ValueError("cannot change an already announced local variable id")
            our_var = _new_variable_id(exclude=incoming_host_ids)
            if header.dst not in (0, SESSION_VAR, our_var):
                raise ValueError(
                    "incoming Pia destination does not match the non-colliding session id"
                )

        self.manager.learn_ids(
            our_var,
            header.src if header.src not in (0, SESSION_VAR) else None,
        )
        if message.proto == PROTO_SESSION:
            self._receive_assignment(message.payload, now=now)
            if message.payload[:1] == b"\x05":
                self._receive_update(message.payload, now=now)
                return  # Never pass unvalidated fragments to the legacy reader.
        self.manager.on_message(message.proto, message.payload, tick=tick, now=now)

    def _receive_diagnostic(self, kind: str, outcome: str, *, now: float | None) -> None:
        """Payload-free local handler boundary, not proof of host/RF behavior.

        A negative remaining budget is intentional: native callbacks can win
        before the due scheduler poll. Never poll/reset a timer here.
        """
        deadline = self.manager._join_phase_deadline
        remaining = ("none" if deadline is None else
                     f"{deadline - (self.manager.clock() if now is None else now):.6f}")
        self.manager.log(
            f"Pia Session RX kind={kind} outcome={outcome} "
            f"phase={self.manager.join_phase} remaining_s={remaining} "
            f"assignment_installed={str(self.assignment_sequence is not None).lower()} "
            f"chunks={len(self._updates.parts)}/{self._updates.count}"
        )

    def _receive_assignment(self, payload: bytes, *, now: float | None = None) -> None:
        """Validate type-2/reason-1, not arbitrary Join replies, as assignment.

        Native writer main+0x2403940 emits exactly 0x25 bytes. Receiver
        0x2404C60/0x2404E9C checks both complete station identities before
        0x2404EE0 stops the Join job and installs body[32] as local index.
        Other reasons include rejection and retry/identity-recovery paths;
        none may be treated as success here. The 32-bit recipient mask
        bounds usable indices; 0xFD is the native unassigned sentinel.
        """
        if payload[:1] != b"\x02":
            return
        if (len(payload) != 0x25 or payload[3] != 1
                or payload[32] >= 32 or self.station_index is not None
                or self.manager.host_var is None):
            self._receive_diagnostic("assignment", "ignored_shape_reason_or_state", now=now)
            return
        host_constant = self.manager.host_mac
        if len(host_constant) == 6:
            host_constant += bytes(2)
        host_identity = host_constant + self.manager.host_var.to_bytes(2, "big")
        our_identity = self.manager.our_mac + self.our_var.to_bytes(2, "big")
        if payload[12:22] != host_identity or payload[22:32] != our_identity:
            self._receive_diagnostic("assignment", "identity_mismatch", now=now)
            return
        if self.manager.acknowledge_session_assignment(now=now):
            self.station_index = payload[32]
            self.station_generation = int.from_bytes(payload[33:35], "big")
            self.assignment_sequence = int.from_bytes(payload[35:37], "big")
            self._update_generation = self.station_generation
            self._receive_diagnostic("assignment", "accepted", now=now)
        else:
            self._receive_diagnostic("assignment", "no_outstanding_join", now=now)

    def _receive_update(self, payload: bytes, *, now: float | None = None) -> None:
        # ROM 0x2405F70 ignores Updates until type 2 installed P+0x1B6;
        # 0x2406250 rejects sequences preceding that assignment baseline.
        if self.assignment_sequence is None:
            self._receive_diagnostic("update", "no_assignment", now=now)
            return
        if len(payload) < 3:
            self._receive_diagnostic("update", "short_header", now=now)
            return
        sequence = int.from_bytes(payload[1:3], "big")
        if sequence < self.assignment_sequence:
            self._receive_diagnostic("update", "before_assignment_sequence", now=now)
            return
        assembled = self._updates.feed(payload)
        if assembled is None:
            # Assembler intentionally does not distinguish incomplete from
            # rejected chunks. Do not label every None a parse rejection.
            self._receive_diagnostic("update", "incomplete_or_invalid_fragment", now=now)
            return
        update = parse_update(assembled)
        if update is None:
            self._receive_diagnostic("update", "invalid_table", now=now)
            return
        host_identity = self.manager.host_mac.ljust(8, b"\0") + self.host_var.to_bytes(2, "big")
        our_identity = self.manager.our_mac + self.our_var.to_bytes(2, "big")
        local = next((s for s in update.stations if s.identity == our_identity), None)
        if (update.host_identity != host_identity or local is None
                or local.index != self.station_index
                or local.generation != self.station_generation
                or not any(s.identity == host_identity for s in update.stations)):
            self._receive_diagnostic("update", "identity_or_install_mismatch", now=now)
            return
        # Commit state only after complete reassembly, host identity and local
        # station installation checks (0x2406320/0x2406650). No guessed slot.
        self.update_sequence = update.sequence
        self.stations = update.stations
        self._update_generation = max(self._update_generation, update.generation,
                                      *(s.generation for s in update.stations))
        self.manager.acknowledge_session_update(
            sequence=update.sequence, generation=self._update_generation, now=now)
        self._receive_diagnostic("update", "accepted", now=now)

    def advance(self, tick: int, *, now: float | None = None) -> None:
        """Join uses elapsed seconds/clock; only RTT retains its tick cadence."""
        self.manager.maybe_retry_session_join(now=now)
        self.manager.maybe_originate_rtt(tick)

    def drain(self) -> tuple[OutboundPiaMessage, ...]:
        return tuple(
            OutboundPiaMessage(
                protocol=item["proto"],
                payload=bytes(item["payload"]),
                destination=item["dst"],
                source=item["src"],
                compress=item["compress"],
                footer=item["footer"],
                establishing=item["establishing"],
                unicast=item["unicast"],
                packet_id=item["pktid"],
                footer_destination=item["footer_var"],
            )
            for item in self.manager.drain()
        )


__all__ = [
    "AdvertisementError",
    "LDN_APPLICATION_VERSION_END",
    "LDN_APPLICATION_VERSION_OFFSET",
    "LDN_SYSTEM_COMMUNICATION_VERSION",
    "LDN_SYSTEM_PROPERTY_SIZE",
    "MAX_VARIABLE_ID",
    "MIN_VARIABLE_ID",
    "OutboundPiaMessage",
    "ZA_PROTOCOL_VERSIONS",
    "ZASession",
    "application_communication_version",
]
