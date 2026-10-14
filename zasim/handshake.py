"""Active, title-payload-free Z-A Pia handshake diagnostic.

This module deliberately stops below Reliable/BoxTrade.  It may answer the
generic Pia Net, Session, and RTT protocols, but it has no trade engine and
fails closed if a future session implementation ever queues another protocol.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
import time
from typing import Callable

from zasim.ldn.pia_connect import (
    NET_CONN_REQUEST,
    NET_CONN_RESPONSE,
    NET_KEEPALIVE_ACK,
    NET_KEEPALIVE_REQUEST,
    PROTO_NET,
    PROTO_RTT,
    PROTO_SESSION,
    SESSION_JOIN_REQUEST,
    SessionPhaseTimeout,
    SESSION_UPDATE,
    parse_net,
    parse_rtt,
    parse_session,
)
from zasim.ldn.transport import LiveTransport, PIA_PORT

from .capture import CaptureWriter, capture_file_identity
from .client import _LivePacketWriter
from .compatibility import DEFAULT_ZA_TITLE_VERSION, identification_token_for_title
from .transport_setup import make_local_transport
from .control import (
    CancellationToken,
    raise_if_cancelled,
    start_transport,
    validate_cancel_event,
)
from .link_code import advertised_link_code_matches, validate_link_code
from .pia import PiaCrypto, PiaHeader, decode_datagram
from .profile import (
    ZA_BOX_TRADE_MAX_PARTICIPANTS,
    ZA_BOX_TRADE_SCENE_ID,
    ZA_PROFILE,
)
from .relay import DatagramTransport, RelayTransport
from .session import ZASession


_HANDSHAKE_PROTOCOLS = frozenset((PROTO_NET, PROTO_RTT, PROTO_SESSION))


class HandshakeSafetyError(RuntimeError):
    """The diagnostic was about to cross its generic-protocol boundary."""


def _positive_seconds(value: float, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def _capture_target(
    output: str | Path | None,
    *,
    overwrite: bool,
    expected_output_identity: tuple[int, int, int, int] | None,
) -> tuple[Path | None, tuple[int, int, int, int] | None]:
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
            raise TypeError(
                "expected_output_identity must be a four-integer tuple"
            )
        if not overwrite:
            raise ValueError("expected_output_identity requires overwrite=True")
        if output is None:
            raise ValueError("expected_output_identity requires an output path")
    if output is None:
        return None, None

    target = Path(output).expanduser()
    if target.suffix.lower() != ".jsonl":
        raise ValueError("handshake capture output must use the .jsonl extension")
    target.parent.mkdir(parents=True, exist_ok=True)
    identity = capture_file_identity(target)
    if (
        expected_output_identity is not None
        and identity != expected_output_identity
    ):
        raise FileExistsError(
            "handshake capture output changed after approval; refusing to "
            f"overwrite: {target}"
        )
    if identity is not None and not overwrite:
        raise FileExistsError(
            "handshake capture output already exists "
            f"(pass overwrite=True/--force): {target}"
        )
    return target, identity


def run_handshake_diagnostic(
    *,
    output: str | Path | None = None,
    overwrite: bool = False,
    expected_output_identity: tuple[int, int, int, int] | None = None,
    timeout: float | None = None,
    duration: float | None = None,
    settle_grace: float = 0.25,
    link_code: str | None = None,
    nickname: str = "ZA-Diag",
    keys_path: str | None = "~/.switch/prod.keys",
    relay: str | None = None,
    phyname: str = "phy0",
    ifname: str = "ldnclient",
    single_interface: bool | None = None,
    bss_helper: str | None = None,
    transport_factory: Callable[[], DatagramTransport] | None = None,
    identification_token: bytes | bytearray | memoryview | None = None,
    title_version: str = DEFAULT_ZA_TITLE_VERSION,
    log: Callable[[str], None] = print,
    cancel_event: CancellationToken | None = None,
) -> dict[str, object]:
    """Join one room and prove only the generic Pia handshake.

    ``duration`` is a compatibility alias used by the audited seeded-BSS
    wrapper.  Supplying both it and ``timeout`` is rejected.  Completion means
    that an authenticated Net 0x11 was answered with Net 0x12 plus Session Join,
    a Session Update was answered with Session Finalize, and a host RTT request
    was answered.  Reliable protocol 10 is never fed or emitted.
    Research-only protocol overrides and Join mutation modes are not part of
    this public diagnostic. Its wire path is the standard session path only.
    """

    # Resolve before output/transport work; mutable overrides cannot drift on start.
    identification_token = identification_token_for_title(title_version, identification_token)
    if timeout is not None and duration is not None:
        raise ValueError("timeout and duration are mutually exclusive")
    effective_timeout = _positive_seconds(
        15.0 if timeout is None and duration is None else (
            duration if duration is not None else timeout
        ),
        "timeout",
    )
    if (
        isinstance(settle_grace, bool)
        or not isinstance(settle_grace, (int, float))
        or not math.isfinite(float(settle_grace))
        or float(settle_grace) < 0
    ):
        raise ValueError("settle_grace must be a finite non-negative number")
    settle_grace = float(settle_grace)
    if single_interface is not None and not isinstance(single_interface, bool):
        raise TypeError("single_interface must be a bool or None")
    if bss_helper is not None and (relay is not None or transport_factory is not None):
        raise ValueError("BSS helper must be configured on the local Linux radio owner, not a relay client or injected transport")
    if transport_factory is not None and relay is not None:
        raise ValueError("transport_factory and relay are mutually exclusive")
    if not callable(log):
        raise TypeError("log must be callable")
    validate_cancel_event(cancel_event)
    raise_if_cancelled(cancel_event)
    target, target_identity = _capture_target(
        output,
        overwrite=overwrite,
        expected_output_identity=expected_output_identity,
    )
    room_code = validate_link_code(link_code) if link_code is not None else None
    if room_code is None and transport_factory is None:
        raise ValueError("live Z-A handshake requires an 8-digit Link Code")

    transport: DatagramTransport
    if transport_factory is not None:
        transport = transport_factory()
    elif relay is not None:
        transport = RelayTransport(
            relay,
            link_code=room_code,
            nickname=nickname,
            communication_id=ZA_PROFILE.title_id_int,
            scene_id=ZA_BOX_TRADE_SCENE_ID,
            expected_max_participants=ZA_BOX_TRADE_MAX_PARTICIPANTS,
            log=log,
        )
    else:
        if os.name == "nt":
            raise RuntimeError(
                "direct LDN handshake requires Linux; on Windows start "
                "relay-serve in WSL2/Linux and pass --relay HOST:PORT"
            )
        if not keys_path:
            raise ValueError("direct Linux LDN handshake requires keys_path")
        transport = make_local_transport(
            LiveTransport, link_code=room_code,
            nickname=nickname, keys_path=keys_path,
            phyname=phyname, ifname=ifname, log=log,
            single_interface=single_interface,
            **({"bss_helper": bss_helper} if bss_helper is not None else {}),
        )

    counters = {
        "packets_received": 0,
        "packets_sent": 0,
        "decode_failures": 0,
        "ignored_peer_packets": 0,
        "ignored_premature_messages": 0,
        "net_requests_received": 0,
        "net_responses_sent": 0,
        "net_keepalive_requests_received": 0,
        "net_keepalive_acks_sent": 0,
        "session_updates_received": 0,
        "session_joins_sent": 0,
        "session_finalizes_sent": 0,
        "rtt_requests_received": 0,
        "rtt_responses_received": 0,
        "rtt_requests_sent": 0,
        "rtt_responses_sent": 0,
        "ignored_title_messages": 0,
    }
    session_type_counts = [0] * 0x100
    started_at: float | None = None
    capture_writer: CaptureWriter | None = None
    connected = False
    success_at: float | None = None
    session: ZASession | None = None
    session_phase_timeout: str | None = None
    try:
        start_transport(transport, cancel_event)
        raise_if_cancelled(cancel_event)
        if room_code is not None and not advertised_link_code_matches(
            transport.app_data, room_code
        ):
            raise HandshakeSafetyError(
                "joined LDN room does not advertise the requested Link Code"
            )
        crypto = PiaCrypto(transport.ssid, profile=ZA_PROFILE)
        writer = _LivePacketWriter(transport, crypto)
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
        )
        if target is not None:
            capture_writer = CaptureWriter(
                target,
                overwrite=overwrite,
                expected_identity=target_identity,
            )
            capture_writer.session(
                ssid=transport.ssid,
                our_ip=transport.our_ip,
                host_ip=transport.host_ip,
                comm_id=int(
                    getattr(transport, "LOCAL_COMMUNICATION_ID", ZA_PROFILE.title_id_int)
                ),
                scene_id=int(
                    getattr(transport, "SCENE_ID", ZA_BOX_TRADE_SCENE_ID)
                ),
                app_version=getattr(transport, "network_app_version", None),
                application_data=transport.app_data,
            )

        started_at = time.monotonic()
        while True:
            raise_if_cancelled(cancel_event)
            now = time.monotonic() - started_at
            if now >= effective_timeout:
                break
            tick = int(now * 60)
            batch = transport.recv()
            for datagram, source_ip in batch:
                counters["packets_received"] += 1
                if capture_writer is not None:
                    capture_writer.packet(
                        "in",
                        bytes(datagram),
                        src=f"{source_ip}:{PIA_PORT}",
                        dst=f"{transport.our_ip}:{PIA_PORT}",
                    )
                # The room key authenticates payloads, not the selected peer.
                # Only the host selected by the LDN transport can provide this
                # diagnostic's handshake evidence. Keep unrelated packets in
                # the private capture, but do not learn ids or count milestones.
                if source_ip != transport.host_ip:
                    counters["ignored_peer_packets"] += 1
                    continue
                try:
                    decoded = decode_datagram(
                        datagram,
                        source_ip,
                        profile=ZA_PROFILE,
                        crypto=crypto,
                    )
                except Exception as exc:
                    counters["decode_failures"] += 1
                    log(
                        "[handshake] authenticated packet decode failed "
                        f"({type(exc).__name__})"
                    )
                    continue

                for message in decoded.messages:
                    # recv() can return several already-buffered packets. A
                    # reply received before its request was actually sent must
                    # not retroactively prove that request after draining the
                    # outbox. Count only replies following successful sends.
                    if ((message.proto == PROTO_SESSION
                            and not counters["session_joins_sent"])
                            or (message.proto == PROTO_RTT
                                and not counters["session_finalizes_sent"])):
                        counters["ignored_premature_messages"] += 1
                        continue
                    session_message = (
                        parse_session(message.payload)
                        if message.proto == PROTO_SESSION
                        else None
                    )
                    if message.proto == PROTO_NET:
                        net = parse_net(message.payload)
                        if net is not None and net[1] == NET_CONN_REQUEST:
                            counters["net_requests_received"] += 1
                        elif net is not None and net[1] == NET_KEEPALIVE_REQUEST:
                            counters["net_keepalive_requests_received"] += 1
                    elif message.proto == PROTO_SESSION:
                        if session_message is not None:
                            session_type = session_message["type"]
                            session_type_counts[session_type] += 1
                            if session_type == SESSION_UPDATE:
                                counters["session_updates_received"] += 1
                    elif message.proto == PROTO_RTT:
                        rtt = parse_rtt(message.payload)
                        if rtt is not None and rtt["type"] == 0:
                            counters["rtt_requests_received"] += 1
                        elif rtt is not None and rtt["type"] == 1:
                            counters["rtt_responses_received"] += 1
                    else:
                        # In particular, never feed Reliable (10) to a title
                        # endpoint during this diagnostic.
                        counters["ignored_title_messages"] += 1
                        continue
                    session.feed(decoded.header, message, tick=tick)

            session.advance(tick)
            for item in session.drain():
                session_message = (
                    parse_session(item.payload)
                    if item.protocol == PROTO_SESSION
                    else None
                )
                is_session_join = bool(
                    session_message is not None
                    and session_message["type"] == SESSION_JOIN_REQUEST
                )
                if item.protocol not in _HANDSHAKE_PROTOCOLS:
                    raise HandshakeSafetyError(
                        "handshake-only session queued a non-generic Pia "
                        f"protocol ({item.protocol}); refusing to send"
                    )
                datagram, destination_ip = writer.encode(item)
                # Trace formatting requires a complete fixed Pia header; it
                # must not weaken the protocol/send boundary above.
                if is_session_join and len(datagram) >= 29:
                    header = PiaHeader.unpack(datagram)
                    protocol_count = item.payload[1] if len(item.payload) >= 2 else 0
                    protocol_end = min(len(item.payload), 2 + 2 * protocol_count)
                    app_version = (
                        item.payload[protocol_end:protocol_end + 2].hex()
                        if len(item.payload) >= protocol_end + 2
                        else ""
                    )
                    log(
                        "[diag] PIA TX SessionJoin "
                        f"hdr_version={header.header_version} "
                        f"dst_var=0x{header.dst:04x} src_var=0x{header.src:04x} "
                        f"packet_id={header.packet_id} nonce={header.nonce.hex()} "
                        f"flags=0x{header.flags:x} gcm_tag={header.tag.hex()} "
                        f"protocols={item.payload[2:protocol_end].hex()} "
                        f"app_version={app_version} plaintext_len={len(item.payload)} "
                        f"ciphertext_len={len(datagram)} udp_dst={destination_ip}:{PIA_PORT}"
                    )
                transport.send(datagram, destination_ip)
                counters["packets_sent"] += 1
                if capture_writer is not None:
                    capture_writer.packet(
                        "out",
                        datagram,
                        src=f"{transport.our_ip}:{PIA_PORT}",
                        dst=f"{destination_ip}:{PIA_PORT}",
                    )

                if item.protocol == PROTO_NET:
                    net = parse_net(item.payload)
                    if net is not None and net[1] == NET_CONN_RESPONSE:
                        counters["net_responses_sent"] += 1
                    elif net is not None and net[1] == NET_KEEPALIVE_ACK:
                        counters["net_keepalive_acks_sent"] += 1
                elif item.protocol == PROTO_SESSION:
                    if session_message is not None:
                        if session_message["type"] == SESSION_JOIN_REQUEST:
                            counters["session_joins_sent"] += 1
                        elif session_message["type"] == 6:
                            counters["session_finalizes_sent"] += 1
                else:
                    rtt = parse_rtt(item.payload)
                    if rtt is not None and rtt["type"] == 0:
                        counters["rtt_requests_sent"] += 1
                    elif rtt is not None and rtt["type"] == 1:
                        counters["rtt_responses_sent"] += 1

            connected = bool(session.connected)
            completed = bool(
                connected
                and counters["net_requests_received"]
                and counters["net_responses_sent"]
                and counters["session_joins_sent"]
                and counters["session_updates_received"]
                and counters["session_finalizes_sent"]
                and counters["rtt_requests_received"]
                and counters["rtt_responses_sent"]
            )
            if completed:
                if success_at is None:
                    success_at = now
                    log(
                        "[handshake] Net/Session/RTT accepted; no Reliable or "
                        "BoxTrade payload was sent"
                    )
                if now - success_at >= settle_grace:
                    break
            if not batch:
                time.sleep(1 / 120)
    except SessionPhaseTimeout as exc:
        session_phase_timeout = exc.phase
        log(f"[handshake] {exc}; stopping without title traffic")
    finally:
        try:
            if capture_writer is not None:
                capture_writer.close()
        finally:
            # A diagnostic-output failure must never suppress radio/relay
            # restoration.
            transport.stop()

    elapsed = 0.0 if started_at is None else time.monotonic() - started_at
    rtt_samples = ()
    if session is not None:
        raw_samples = getattr(session.manager, "rtt_samples", ())
        rtt_samples = tuple(int(sample) for sample in raw_samples)
    completed = success_at is not None
    required_milestones = [
        ("Net 0x11 received", counters["net_requests_received"]),
        ("Net 0x12 sent", counters["net_responses_sent"]),
        ("Session Join sent", counters["session_joins_sent"]),
        ("Session Update received", counters["session_updates_received"]),
        ("Session Finalize sent", counters["session_finalizes_sent"]),
        ("RTT request received", counters["rtt_requests_received"]),
        ("RTT response sent", counters["rtt_responses_sent"]),
        ("session connected", connected),
    ]
    print(
        "[diag] handshake milestones "
        + ", ".join(
            f"{name}={int(bool(value))}"
            for name, value in required_milestones
        )
    )
    return {
        "handshake_only": True,
        "completed": completed,
        "elapsed_seconds": elapsed,
        "output": str(target) if target is not None else None,
        "connected": connected,
        "rtt_round_trips": len(rtt_samples),
        "rtt_samples_ticks": list(rtt_samples),
        "missing_milestones": [
            name for name, reached in required_milestones if not reached
        ],
        "reliable_messages_sent": 0,
        "boxtrade_commands_sent": 0,
        "session_phase_timeout": session_phase_timeout,
        "session_message_type_counts": {
            str(message_type): count
            for message_type, count in enumerate(session_type_counts)
            if count
        },
        **counters,
    }


__all__ = [
    "HandshakeSafetyError",
    "run_handshake_diagnostic",
]
