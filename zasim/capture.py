"""JSONL capture helpers for clean-room Pokemon Legends: Z-A protocol work.

The capture format deliberately stores the untouched UDP payload next to enough
session metadata to authenticate/decode Pia packets later.  It never stores or
prints the contents of ``prod.keys``.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from typing import Any, Iterable, Iterator

from .control import CancellationToken, raise_if_cancelled, validate_cancel_event
from .file_save import open_regular_binary
from .json_limits import validate_json_depth


FORMAT = "zatrade-pia-jsonl-v1"
MAX_CAPTURE_LINE_BYTES = 64 * 1024


def capture_file_identity(path: str | Path) -> tuple[int, int, int, int] | None:
    target = Path(path)
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"capture output is not a regular file: {target}")
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def endpoint_ip(endpoint: str | None) -> str | None:
    """Return the IP portion of an ``IP:port`` endpoint used by our JSONL files."""
    if endpoint is None:
        return None
    if not isinstance(endpoint, str):
        raise ValueError("packet endpoint must be text or null")
    if not endpoint:
        return None
    return endpoint.rsplit(":", 1)[0] if ":" in endpoint else endpoint


class CaptureWriter:
    """Line-buffered writer for session metadata and raw Pia UDP datagrams."""

    def __init__(
        self,
        path: str | Path,
        *,
        overwrite: bool = False,
        expected_identity: tuple[int, int, int, int] | None = None,
    ):
        if not isinstance(overwrite, bool):
            raise TypeError("overwrite must be a bool")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if overwrite and expected_identity is not None:
            flags = os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
        else:
            # Exclusive creation protects an output that was absent during
            # preflight from a late path/symlink swap.
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        # A regular overwrite target can be exchanged for a FIFO after
        # approval. Check the opened descriptor, but do not block in open()
        # before that check has a chance to reject it.
        flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("capture output is not a regular file")
            if overwrite and expected_identity is not None:
                opened_identity = (
                    int(metadata.st_dev),
                    int(metadata.st_ino),
                    int(metadata.st_size),
                    int(metadata.st_mtime_ns),
                )
                if opened_identity != expected_identity:
                    raise FileExistsError(
                        f"capture output changed after approval; refusing to overwrite: "
                        f"{self.path}"
                    )
                os.ftruncate(descriptor, 0)
            self._fp = os.fdopen(
                descriptor, "w", encoding="utf-8", buffering=1
            )
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        self._started_ns = time.time_ns()

    def _write(self, record: dict[str, Any]) -> None:
        self._fp.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def session(self, *, ssid: bytes, our_ip: str, host_ip: str,
                comm_id: int, scene_id: int, app_version: int | None = None,
                application_data: bytes | None = None) -> None:
        self._write({
            "rec": "meta",
            "event": "session",
            "format": FORMAT,
            "created_unix_ns": self._started_ns,
            "title": "Pokemon Legends: Z-A",
            "comm_id": f"0x{comm_id:016X}",
            "scene_id": scene_id,
            "app_version": app_version,
            "ssid_hex": bytes(ssid).hex(),
            "ip": our_ip,
            "host_ip": host_ip,
            "application_data_hex": (bytes(application_data).hex()
                                     if application_data is not None else None),
        })

    def packet(self, direction: str, datagram: bytes, *, src: str, dst: str) -> None:
        if direction not in {"in", "out"}:
            raise ValueError("direction must be 'in' or 'out'")
        self._write({
            "rec": "pkt",
            "dir": direction,
            "elapsed_ns": time.time_ns() - self._started_ns,
            "src": src,
            "dst": dst,
            "len": len(datagram),
            "hex": bytes(datagram).hex(),
        })

    def close(self) -> None:
        if not self._fp.closed:
            self._fp.close()

    def __enter__(self) -> "CaptureWriter":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def iter_records(
    path: str | Path, *, cancel_event: CancellationToken | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield valid JSON objects, reporting malformed non-empty lines explicitly."""
    # CaptureWriter emits compact records of only a few KiB for a maximum-size
    # Pia datagram.  A generous fixed bound retains those and old normal
    # captures while preventing a hostile single-line JSON/hex allocation.
    validate_cancel_event(cancel_event)
    raise_if_cancelled(cancel_event)
    with open_regular_binary(path, allow_symlink=True) as fp:
        line_no = 0
        while True:
            raise_if_cancelled(cancel_event)
            raw = fp.readline(MAX_CAPTURE_LINE_BYTES + 1)
            raise_if_cancelled(cancel_event)
            if not raw:
                break
            line_no += 1
            if len(raw) > MAX_CAPTURE_LINE_BYTES:
                raise ValueError(
                    f"{path}:{line_no}: capture line exceeds "
                    f"{MAX_CAPTURE_LINE_BYTES} bytes"
                )
            if not raw.strip():
                continue
            try:
                validate_json_depth(raw)
                record = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError as exc:
                reason = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
                raise ValueError(f"{path}:{line_no}: invalid JSON: {reason}") from exc
            except RecursionError as exc:
                raise ValueError(f"{path}:{line_no}: JSON nesting is too deep") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_no}: capture record is not an object")
            yield record


def trade_command_summary(command: Any) -> dict[str, Any]:
    """Return a compact, JSON-safe summary of one decoded Z-A trade command.

    Ready's 0x4B0-byte matrix and SelectPokemon's PA9 can contain bulky or
    user-specific data, so passive analysis reports their size/fingerprint
    rather than echoing the complete binary value.
    """
    from .trade_protocol import (
        CancelTrade,
        ConfirmTrade,
        FinalAgreement,
        Ready,
        SelectPokemon,
        command_id,
    )

    effective_id = command_id(command)
    summary: dict[str, Any] = {
        "name": effective_id.name.lower(),
        "effective_id": f"0x{int(effective_id):03X}",
        "command_index": effective_id.command_index,
        "sequence": command.sequence,
    }
    if isinstance(command, Ready):
        matrix = bytes(command.matrix)
        summary["compatibility"] = {
            "bytes": len(matrix),
            "set_bits": sum(value.bit_count() for value in matrix),
            "sha256": hashlib.sha256(matrix).hexdigest(),
        }
    elif isinstance(command, SelectPokemon):
        summary["preview"] = command.preview
        summary["party_pa9"] = {
            "bytes": len(command.party_pa9),
            "sha256": hashlib.sha256(command.party_pa9).hexdigest(),
        }
    elif isinstance(command, CancelTrade):
        summary["reason"] = command.reason
    elif not isinstance(command, (ConfirmTrade, FinalAgreement)):
        raise TypeError(f"unsupported trade command type: {type(command).__name__}")
    return summary


def trade_archive_summary(
    endpoint: int, command_index: int, archive: bytes | bytearray | memoryview
) -> dict[str, Any]:
    """Best-effort decode of a command archive with an explicitly supplied prefix.

    This helper deliberately returns an ``error`` field for malformed input.
    ``endpoint`` and ``command_index`` are title application fields, not Pia
    compact-message metadata.  Use :func:`trade_wire_payload_summary` when the
    two-byte prefix is already present in the Reliable application payload.
    """
    from .trade_protocol import TradeFrame, decode_frame

    result: dict[str, Any] = {
        "endpoint": endpoint,
        "command_index": command_index,
        "effective_id": f"0x{0x100 | command_index:03X}",
        "archive_bytes": len(archive),
    }
    try:
        frame = TradeFrame.from_descriptor(
            bytes((endpoint, command_index)), bytes(archive)
        )
        result["command"] = trade_command_summary(decode_frame(frame))
        result["recognized"] = True
    except (TypeError, ValueError) as exc:
        result["recognized"] = False
        result["error"] = str(exc)
    return result


def trade_wire_payload_summary(
    payload: bytes | bytearray | memoryview,
) -> dict[str, Any]:
    """Best-effort decode of ``endpoint,index || archive`` from Reliable."""
    from .trade_protocol import TradeFrame, decode_frame

    raw = bytes(payload)
    result: dict[str, Any] = {"wire_payload_bytes": len(raw)}
    if len(raw) >= 1:
        result["endpoint"] = raw[0]
    if len(raw) >= 2:
        result["command_index"] = raw[1]
        result["effective_id"] = f"0x{0x100 | raw[1]:03X}"
        result["archive_bytes"] = len(raw) - 2
    try:
        frame = TradeFrame.from_wire_payload(raw)
        result["command"] = trade_command_summary(decode_frame(frame))
        result["recognized"] = True
    except (TypeError, ValueError) as exc:
        result["recognized"] = False
        result["error"] = str(exc)
    return result


def trade_frame_summary(
    port: int, protocol_specific: int, payload: bytes | bytearray | memoryview
) -> dict[str, Any]:
    """Compatibility wrapper for the old, incorrectly named helper.

    The first two arguments are interpreted as the title's application
    ``endpoint`` and ``command_index``.  They are not Pia compact metadata.
    New code should call :func:`trade_archive_summary`.
    """
    return trade_archive_summary(port, protocol_specific, payload)


def _application_message_summary(
    payload: bytes,
    preview: int,
    *,
    first_sequence: int,
    last_sequence: int,
    fragment_count: int,
    compressed: bool,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "first_sequence": first_sequence,
        "last_sequence": last_sequence,
        "fragment_count": fragment_count,
        "compressed": compressed,
        "size": len(payload),
        "payload_hex": payload[:preview].hex(),
        "truncated": len(payload) > preview,
    }
    # Compression is a Reliable-layer concern.  Never interpret compressed
    # bytes as the title application prefix.
    if not compressed and len(payload) >= 2 and payload[0] == 1 and payload[1] <= 4:
        summary["trade"] = trade_wire_payload_summary(payload)
    return summary


def _message_summary(message: Any, preview: int, reassembler: Any = None) -> dict[str, Any]:
    payload = bytes(getattr(message, "payload", b""))
    port = getattr(message, "port", None)
    protocol_specific = getattr(message, "protocol_specific", None)
    summary = {
        "proto": getattr(message, "proto", None),
        "flags": getattr(message, "flags", None),
        "msgflags": getattr(message, "msgflags", None),
        "port": port,
        "protocol_specific": protocol_specific,
        "size": len(payload),
        "payload_hex": payload[:preview].hex(),
        "truncated": len(payload) > preview,
    }
    if getattr(message, "proto", None) == 10:
        from .reliable import ReliableFrame, ReliableReassembler

        try:
            frame = ReliableFrame.unpack(payload)
            reliable: dict[str, Any] = {
                "flags": int(frame.flags),
                "sequence_id": frame.sequence_id,
                "lowest_pending_ack": frame.lowest_pending_ack,
                "destination_bit_count": frame.destination_bit_count,
                "bitmap_words": [f"0x{word:08X}" for word in frame.bitmap_words],
                "payload_bytes": len(frame.payload),
                "application_data": frame.is_application_data,
            }
            if reassembler is None:
                # A complete one-frame message can be summarized without any
                # stream history.  Fragmented messages require capture state.
                if frame.is_complete_message:
                    reliable["application_messages"] = [
                        _application_message_summary(
                            frame.payload,
                            preview,
                            first_sequence=frame.sequence_id,
                            last_sequence=frame.sequence_id,
                            fragment_count=1,
                            compressed=bool(frame.flags & 0x10),
                        )
                    ]
            else:
                if not isinstance(reassembler, ReliableReassembler):
                    raise TypeError("reassembler must be a ReliableReassembler")
                completed = reassembler.feed(frame)
                if completed:
                    reliable["application_messages"] = [
                        _application_message_summary(
                            item.payload,
                            preview,
                            first_sequence=item.first_sequence,
                            last_sequence=item.last_sequence,
                            fragment_count=item.fragment_count,
                            compressed=item.compressed,
                        )
                        for item in completed
                    ]
            summary["reliable"] = reliable
        except (TypeError, ValueError) as exc:
            summary["reliable"] = {"recognized": False, "error": str(exc)}
    return summary


def decoded_summary(
    decoded: Any,
    *,
    preview: int = 48,
    reliable_reassemblers: dict[Any, Any] | None = None,
    reliable_stream: Any = None,
) -> dict[str, Any]:
    """Convert a ``zasim.pia.DecodedDatagram`` to stable JSON-friendly fields."""
    header = getattr(decoded, "header", None)
    if is_dataclass(header):
        hdr = asdict(header)
    elif header is not None:
        hdr = {
            name: getattr(header, name)
            for name in ("enc", "flags", "dst", "src", "pktid", "footer_size", "footer")
            if hasattr(header, name)
        }
    else:
        hdr = None
    if hdr:
        for key, value in tuple(hdr.items()):
            if isinstance(value, (bytes, bytearray)):
                hdr[key] = bytes(value).hex()
    footer = bytes(getattr(decoded, "footer", b""))
    message_bytes = bytes(getattr(decoded, "message_bytes", b""))
    messages = []
    for message in getattr(decoded, "messages", None) or []:
        reassembler = None
        if reliable_reassemblers is not None and getattr(message, "proto", None) == 10:
            from .reliable import ReliableReassembler

            # Reliable sequence space belongs to the peer/direction stream.
            # Compact header fields are deliberately not treated as title
            # endpoint identity (nor used to split application fragments).
            try:
                reassembler = reliable_reassemblers[reliable_stream]
            except KeyError:
                reassembler = ReliableReassembler()
                reliable_reassemblers[reliable_stream] = reassembler
        messages.append(_message_summary(message, preview, reassembler))
    return {
        "header": hdr,
        "compressed": bool(getattr(decoded, "compressed", False)),
        "footer_hex": footer.hex(),
        "message_bytes": len(message_bytes),
        "messages": messages,
    }


def analyze_capture(
    path: str | Path, *, preview: int = 48,
    cancel_event: CancellationToken | None = None,
) -> Iterable[dict[str, Any]]:
    """Decrypt and summarize packets from a capture using its session SSID.

    Errors are returned per packet so a partly corrupt or mixed capture remains useful.
    """
    validate_cancel_event(cancel_event)
    raise_if_cancelled(cancel_event)
    from .pia import PiaCrypto, decode_datagram

    crypto = None
    reliable_reassemblers: dict[Any, Any] = {}
    for index, record in enumerate(iter_records(path, cancel_event=cancel_event)):
        raise_if_cancelled(cancel_event)
        if record.get("rec") == "meta" and record.get("event") == "session":
            ssid_hex = record.get("ssid_hex")
            if not isinstance(ssid_hex, str) or not ssid_hex:
                raise ValueError("session metadata must contain a non-empty text ssid_hex")
            crypto = PiaCrypto(bytes.fromhex(ssid_hex))
            reliable_reassemblers.clear()
            yield {"record": index, "type": "session", "session": record}
            continue
        if record.get("rec") != "pkt":
            continue
        result: dict[str, Any] = {
            "record": index,
            "type": "packet",
            "direction": record.get("dir"),
            "src": record.get("src"),
            "dst": record.get("dst"),
        }
        try:
            if crypto is None:
                raise ValueError("packet appears before session metadata")
            datagram = bytes.fromhex(record["hex"])
            src_ip = endpoint_ip(record.get("src"))
            if not src_ip:
                raise ValueError("packet does not contain a source IP")
            decoded = decode_datagram(datagram, src_ip, crypto=crypto)
            stream = (
                record.get("dir"),
                record.get("src"),
                record.get("dst"),
                getattr(decoded.header, "src", None),
                getattr(decoded.header, "dst", None),
            )
            result["decoded"] = decoded_summary(
                decoded,
                preview=preview,
                reliable_reassemblers=reliable_reassemblers,
                reliable_stream=stream,
            )
        except (KeyError, TypeError, ValueError) as exc:
            result["error"] = str(exc)
        yield result
