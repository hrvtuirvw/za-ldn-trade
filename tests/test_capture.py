from dataclasses import dataclass
import json

import pytest

from zasim.capture import (
    MAX_CAPTURE_LINE_BYTES,
    CaptureWriter,
    capture_file_identity,
    decoded_summary,
    endpoint_ip,
    iter_records,
)
from zasim.reliable import ReliableFlags, build_reliable
from zasim.trade_protocol import ConfirmTrade, encode_frame


def test_capture_writer_round_trip(tmp_path):
    path = tmp_path / "capture.jsonl"
    with CaptureWriter(path) as capture:
        capture.session(
            ssid=bytes(range(16)), our_ip="169.254.7.2", host_ip="169.254.7.1",
            comm_id=0x0100F43008C44000, scene_id=0, app_version=1,
            application_data=b"beacon",
        )
        capture.packet("in", b"\x32\xab", src="169.254.7.1:12345",
                       dst="169.254.7.2:12345")

    records = list(iter_records(path))
    assert records[0]["format"] == "zatrade-pia-jsonl-v1"
    assert records[0]["ssid_hex"] == bytes(range(16)).hex()
    assert records[1]["hex"] == "32ab"
    assert records[1]["len"] == 2


def test_capture_writer_refuses_target_changed_after_approval(tmp_path) -> None:
    path = tmp_path / "capture.jsonl"
    path.write_text("approved", encoding="utf-8")
    expected = capture_file_identity(path)
    replacement = tmp_path / "replacement"
    replacement.write_text("new owner", encoding="utf-8")
    path.unlink()
    replacement.replace(path)

    with pytest.raises(FileExistsError, match="changed after approval"):
        CaptureWriter(path, overwrite=True, expected_identity=expected)

    assert path.read_text(encoding="utf-8") == "new owner"


def test_capture_writer_exclusively_reserves_previously_absent_target(tmp_path) -> None:
    path = tmp_path / "capture.jsonl"
    path.write_text("appeared late", encoding="utf-8")

    with pytest.raises(FileExistsError):
        CaptureWriter(path, overwrite=True, expected_identity=None)

    assert path.read_text(encoding="utf-8") == "appeared late"


def test_endpoint_ip():
    assert endpoint_ip("169.254.1.2:12345") == "169.254.1.2"
    assert endpoint_ip("169.254.1.2") == "169.254.1.2"
    assert endpoint_ip(None) is None


@dataclass
class Header:
    dst: int = 1
    src: int = 2
    nonce8: bytes = b"12345678"


@dataclass
class Message:
    proto: int
    flags: int
    payload: bytes
    msgflags: int = 0
    port: int | None = None
    protocol_specific: int | None = None


@dataclass
class Decoded:
    header: Header
    compressed: bool
    footer: bytes
    message_bytes: bytes
    messages: list[Message]


def test_decoded_summary_is_json_safe_and_truncates():
    value = decoded_summary(Decoded(Header(), True, b"\x00\x01", b"abcdef",
                                    [Message(10, 7, b"abcdef")]), preview=3)
    assert value["header"]["nonce8"] == "3132333435363738"
    assert value["messages"][0]["payload_hex"] == "616263"
    assert value["messages"][0]["truncated"] is True
    assert value["messages"][0]["port"] is None
    json.dumps(value)


def test_decoded_summary_recognizes_trade_prefix_after_reliable_header():
    wire_payload = encode_frame(ConfirmTrade(7)).wire_payload
    decoded = Decoded(
        Header(),
        False,
        b"",
        b"",
        [
            Message(
                10,
                0x1F,
                build_reliable(3, 0, wire_payload),
                # These are unrelated Pia compact-message fields.
                port=9,
                protocol_specific=77,
            )
        ],
    )
    message = decoded_summary(decoded)["messages"][0]

    assert message["port"] == 9
    assert message["protocol_specific"] == 77
    application = message["reliable"]["application_messages"][0]
    assert application["trade"]["recognized"] is True
    assert application["trade"]["endpoint"] == 1
    assert application["trade"]["command_index"] == 2
    assert application["trade"]["command"]["name"] == "confirm_trade"
    assert application["trade"]["command"]["sequence"] == 7


def test_malformed_reliable_payload_is_reported_without_aborting_summary():
    decoded = Decoded(
        Header(),
        False,
        b"",
        b"",
        [
            Message(10, 0x1F, b"not-a-reliable-frame", port=1, protocol_specific=3),
            Message(
                10,
                0x1F,
                build_reliable(4, 0, encode_frame(ConfirmTrade(1)).wire_payload),
            ),
        ],
    )
    messages = decoded_summary(decoded)["messages"]

    assert messages[0]["reliable"]["recognized"] is False
    assert "error" in messages[0]["reliable"]
    trade = messages[1]["reliable"]["application_messages"][0]["trade"]
    assert trade["recognized"] is True
    assert trade["command"]["name"] == "confirm_trade"


def test_trade_decoder_is_not_applied_to_compact_metadata_or_non_trade_wire_payload():
    decoded = Decoded(
        Header(),
        False,
        b"",
        b"",
        [
            Message(10, 0, bytes.fromhex("b90100"), port=2, protocol_specific=2),
            Message(10, 0, build_reliable(1, 0, b"\x02\x02b90100"), port=1,
                    protocol_specific=2),
        ],
    )
    messages = decoded_summary(decoded)["messages"]
    assert "trade" not in messages[0]
    assert messages[0]["reliable"]["recognized"] is False
    application = messages[1]["reliable"]["application_messages"][0]
    assert "trade" not in application


def test_decoded_summary_reassembles_before_reading_application_prefix():
    wire = encode_frame(ConfirmTrade(42)).wire_payload
    start = build_reliable(
        10,
        0,
        wire[:2],
        ReliableFlags.APPLICATION_DATA | ReliableFlags.MESSAGE_START,
    )
    end = build_reliable(
        11,
        0,
        wire[2:],
        ReliableFlags.APPLICATION_DATA | ReliableFlags.MESSAGE_END,
    )
    state = {}
    first = decoded_summary(
        Decoded(Header(), False, b"", b"", [Message(10, 0, start)]),
        reliable_reassemblers=state,
        reliable_stream="peer-a",
    )
    assert "application_messages" not in first["messages"][0]["reliable"]

    second = decoded_summary(
        Decoded(Header(), False, b"", b"", [Message(10, 0, end)]),
        reliable_reassemblers=state,
        reliable_stream="peer-a",
    )
    application = second["messages"][0]["reliable"]["application_messages"][0]
    assert application["fragment_count"] == 2
    assert application["trade"]["command"]["sequence"] == 42


def test_iter_records_reports_bad_line(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text("{}\nnot-json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        list(iter_records(path))


def test_iter_records_rejects_oversized_line_without_unbounded_read(tmp_path):
    path = tmp_path / "oversized.jsonl"
    path.write_bytes(b'{"value":"' + (b"a" * MAX_CAPTURE_LINE_BYTES) + b'"}\n')

    with pytest.raises(ValueError, match="capture line exceeds"):
        list(iter_records(path))
