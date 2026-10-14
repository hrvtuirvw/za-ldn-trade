"""Live, passive Z-A LDN probe.

This capture adapter joins one selected room and records incoming datagrams.
It sends no Pia handshake or trade messages; active handshake and trade flows
are separate adapters. LDN association itself is not radio-passive.
"""

from __future__ import annotations

import os
import math
from pathlib import Path
import time
from typing import Callable

from zasim.ldn.transport import LiveTransport, PIA_PORT

from .capture import CaptureWriter, capture_file_identity, decoded_summary
from .transport_setup import make_local_transport
from .control import (
    CancellationToken,
    raise_if_cancelled,
    start_transport,
    validate_cancel_event,
)
from .link_code import advertised_link_code_matches, validate_link_code
from .pia import PiaCrypto, decode_datagram
from .profile import (
    ZA_BOX_TRADE_MAX_PARTICIPANTS,
    ZA_BOX_TRADE_SCENE_ID,
    ZA_PROFILE,
)
from .relay import DatagramTransport, RelayTransport


def _za_beacon(app_data: bytes, log: Callable[[str], None]) -> bytes:
    data = bytes(app_data or b"")
    if data:
        log(f"[probe] LDN application data: {len(data)} bytes, prefix={data[:16].hex()}")
    else:
        log("[probe] LDN advertisement has no application data")
    return data


def run_passive_probe(*, output: str | Path, keys_path: str | None = None,
                      overwrite: bool = False,
                      expected_output_identity: tuple[int, int, int, int] | None = None,
                      duration: float = 30.0, nickname: str = "ZATRADE",
                      phyname: str = "phy0", ifname: str = "ldnclient",
                      comm_id: int | None = None,
                      scene_id: int = ZA_BOX_TRADE_SCENE_ID,
                      link_code: str | None = None,
                      relay: str | None = None,
                      single_interface: bool | None = None,
                      bss_helper: str | None = None,
                      transport_factory: Callable[[], DatagramTransport] | None = None,
                      cancel_event: CancellationToken | None = None,
                      log: Callable[[str], None] = print) -> dict[str, int | str]:
    """Join a Z-A LDN room and save incoming Pia datagrams for offline analysis.

    Direct mode uses kinnay's Linux ``ldn`` package.  Windows can instead use
    the loopback/SSH relay, in which case ``prod.keys`` remains on Linux/WSL2.
    """
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or duration <= 0
    ):
        raise ValueError("duration must be a finite positive number")
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
    if single_interface is not None and not isinstance(single_interface, bool):
        raise TypeError("single_interface must be a bool or None")
    target = Path(output).expanduser()
    if target.suffix.lower() != ".jsonl":
        raise ValueError("capture output must use the .jsonl extension")
    target.parent.mkdir(parents=True, exist_ok=True)
    target_identity = capture_file_identity(target)
    if (
        expected_output_identity is not None
        and target_identity != expected_output_identity
    ):
        raise FileExistsError(
            f"capture output changed after approval; refusing to overwrite: {target}"
        )
    if target_identity is not None and not overwrite:
        raise FileExistsError(
            f"capture output already exists (pass overwrite=True/--force): {target}"
        )
    validate_cancel_event(cancel_event)
    raise_if_cancelled(cancel_event)
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
        raise ValueError("live Z-A probing requires an 8-digit Link Code")

    title_comm_id = ZA_PROFILE.title_id_int if comm_id is None else comm_id
    transport: DatagramTransport
    if transport_factory is not None:
        transport = transport_factory()
    elif relay is not None:
        transport = RelayTransport(
            relay,
            link_code=room_code,
            nickname=nickname,
            communication_id=title_comm_id,
            scene_id=scene_id,
            expected_max_participants=ZA_BOX_TRADE_MAX_PARTICIPANTS,
            log=log,
        )
    else:
        if os.name == "nt":
            raise RuntimeError(
                "direct LDN probing requires Linux; on Windows start "
                "relay-serve in WSL2/Linux and pass --relay HOST:PORT"
            )
        if not keys_path:
            raise ValueError("direct Linux LDN probing requires keys_path")
        transport = make_local_transport(
            LiveTransport, link_code=room_code,
            communication_id=title_comm_id, scene_id=scene_id,
            app_data_decoder=_za_beacon,
            nickname=nickname, keys_path=keys_path,
            phyname=phyname, ifname=ifname, log=log,
            single_interface=single_interface,
            **({"bss_helper": bss_helper} if bss_helper is not None else {}),
        )
    packets = decoded = failures = 0

    try:
        start_transport(transport, cancel_event)
        raise_if_cancelled(cancel_event)
        if room_code is not None and not advertised_link_code_matches(
            transport.app_data, room_code
        ):
            raise RuntimeError(
                "joined LDN room does not advertise the requested Link Code"
            )
        crypto = PiaCrypto(transport.ssid)
        deadline = time.monotonic() + duration
        with CaptureWriter(
            target,
            overwrite=overwrite,
            expected_identity=target_identity,
        ) as capture:
            capture.session(
                ssid=transport.ssid,
                our_ip=transport.our_ip,
                host_ip=transport.host_ip,
                comm_id=transport.LOCAL_COMMUNICATION_ID,
                scene_id=transport.SCENE_ID,
                app_version=transport.network_app_version,
                application_data=transport.app_data,
            )
            log(f"[probe] capturing packets for {duration:g}s -> {target}")
            while time.monotonic() < deadline:
                raise_if_cancelled(cancel_event)
                batch = transport.recv()
                for datagram, src_ip in batch:
                    raise_if_cancelled(cancel_event)
                    if time.monotonic() >= deadline:
                        break
                    packets += 1
                    capture.packet(
                        "in", datagram,
                        src=f"{src_ip}:{PIA_PORT}",
                        dst=f"{transport.our_ip}:{PIA_PORT}",
                    )
                    try:
                        packet = decode_datagram(datagram, src_ip, crypto=crypto)
                        summary = decoded_summary(packet, preview=16)
                        protos = [m["proto"] for m in summary["messages"]]
                        log(f"[probe] packet {packets}: pktid={packet.header.pktid} "
                            f"messages={protos}")
                        decoded += 1
                    except Exception as exc:  # packet stays in the capture for later inspection
                        failures += 1
                        log(f"[probe] packet {packets}: decode failed: {exc}")
                if not batch:
                    time.sleep(0.01)
            raise_if_cancelled(cancel_event)
    except KeyboardInterrupt:
        log("[probe] interrupted; capture was flushed")
    finally:
        transport.stop()

    raise_if_cancelled(cancel_event)

    return {
        "output": str(target),
        "packets": packets,
        "decoded": decoded,
        "decode_failures": failures,
    }
