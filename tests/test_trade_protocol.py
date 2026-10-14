from __future__ import annotations

import pytest

from zasim.trade_protocol import (
    ArchiveError,
    CancelTrade,
    CommandValidationError,
    CompatibilityMatrix,
    ConfirmTrade,
    FinalAgreement,
    Ready,
    SelectPokemon,
    TradeCommandID,
    TradeFrame,
    UnknownCommandError,
    decode_command,
    decode_frame,
    decode_signed,
    decode_varuint,
    encode_blob,
    encode_command,
    encode_frame,
    encode_signed,
    encode_struct,
    encode_varuint,
)


@pytest.mark.parametrize(
    ("value", "wire"),
    [
        (0, "00"),
        (0x7F, "7f"),
        (0x80, "8080"),
        (0xFF, "80ff"),
        (0x100, "810001"),
        (0xFFFF, "81ffff"),
        (0x10000, "8200000100"),
        (0xFFFFFFFF, "82ffffffff"),
        (0x100000000, "830000000001000000"),
        (0xFFFFFFFFFFFFFFFF, "83ffffffffffffffff"),
    ],
)
def test_varuint_known_vectors(value: int, wire: str) -> None:
    encoded = encode_varuint(value)
    assert encoded.hex() == wire
    assert decode_varuint(b"x" + encoded + b"y", 1) == (value, len(encoded) + 1)


@pytest.mark.parametrize(
    ("value", "wire"),
    [
        (-0x80000000, "8600000080"),
        (-32769, "86ff7fffff"),
        (-32768, "850080"),
        (-129, "857fff"),
        (-128, "8480"),
        (-65, "84bf"),
        (-64, "c0"),
        (-1, "ff"),
        (0, "00"),
        (127, "7f"),
        (128, "858000"),
        (32767, "85ff7f"),
        (32768, "8600800000"),
        (0x7FFFFFFF, "86ffffff7f"),
    ],
)
def test_signed_known_vectors(value: int, wire: str) -> None:
    encoded = encode_signed(value)
    assert encoded.hex() == wire
    assert decode_signed(encoded) == (value, len(encoded))


@pytest.mark.parametrize(
    "wire",
    [
        b"",
        b"\x80",
        b"\x81\0",
        b"\x80\x7f",  # should have used the raw one-byte form
        b"\x81\xff\0",  # should have used tag 0x80
        b"\x87",
    ],
)
def test_varuint_rejects_truncated_reserved_and_noncanonical_forms(wire: bytes) -> None:
    with pytest.raises(ArchiveError):
        decode_varuint(wire)


@pytest.mark.parametrize(
    "wire",
    [
        b"",
        b"\x84",
        b"\x85\0",
        b"\x84\xff",  # -1 has a raw one-byte form
        b"\x85\x80\xff",  # -128 uses tag 0x84
        b"\x86\xff\x7f\x00\x00",  # 32767 should use tag 0x85
        b"\x80",
    ],
)
def test_signed_rejects_invalid_forms(wire: bytes) -> None:
    with pytest.raises(ArchiveError):
        decode_signed(wire)


def test_structure_and_blob_markers_are_exact() -> None:
    assert encode_blob(b"abc") == b"\xBC\x03abc"
    assert encode_struct(b"\x01", encode_blob(b"x")) == b"\xB9\x02\x01\xBC\x01x"


def test_compatibility_matrix_boundaries_and_copying() -> None:
    matrix = CompatibilityMatrix.empty()
    assert len(matrix) == 0x4B0
    assert bytes(matrix) == bytes(0x4B0)
    assert not matrix.test(0, 0)

    assert matrix.set(0, 0) is matrix
    matrix.set(0, 23).set(399, 23)
    assert matrix.test(0, 0)
    assert matrix.test(0, 23)
    assert matrix.test(399, 23)
    assert bytes(matrix)[:3] == bytes.fromhex("010080")
    assert bytes(matrix)[-1] == 0x80

    clone = matrix.copy()
    clone.set(0, 0, False)
    assert not clone.test(0, 0)
    assert matrix.test(0, 0)
    assert CompatibilityMatrix.full().test(218, 17)

    with pytest.raises(ValueError):
        CompatibilityMatrix(b"short")
    with pytest.raises(ValueError):
        matrix.test(400, 0)
    with pytest.raises(ValueError):
        matrix.set(0, 24)


def test_ready_exact_archive_and_roundtrip() -> None:
    matrix = CompatibilityMatrix.empty().set(0, 0).set(399, 23)
    command = Ready(0, matrix)
    wire = encode_command(command)

    assert len(wire) == 1209
    assert wire[:9] == bytes.fromhex("b90200b901bc81b004")
    assert wire[9] == 1
    assert wire[-1] == 0x80
    assert decode_command(0x100, wire) == command


def test_select_exact_archive_and_roundtrip() -> None:
    # The codec preserves the already-encrypted party PA9 opaquely.  All-zero
    # bytes remain valid for the game's transient/clear offer path.
    party = bytes((index * 73 + 11) & 0xFF for index in range(0x158))
    command = SelectPokemon(0, party, False)
    wire = encode_command(command)

    assert len(wire) == 352
    assert wire[:7] == bytes.fromhex("b90300bc815801")
    assert wire[7:-1] == party
    assert wire[-1] == 0
    assert decode_command(TradeCommandID.SELECT_POKEMON, wire) == command


@pytest.mark.parametrize(
    ("command", "effective_id", "descriptor", "wire"),
    [
        (ConfirmTrade(0), 0x102, b"\x01\x02", b"\xB9\x01\x00"),
        (CancelTrade(0, 2), 0x103, b"\x01\x03", b"\xB9\x02\x00\x02"),
        (FinalAgreement(0), 0x104, b"\x01\x04", b"\xB9\x01\x00"),
    ],
)
def test_small_commands_and_frame_metadata(
    command: object, effective_id: int, descriptor: bytes, wire: bytes
) -> None:
    frame = encode_frame(command)  # type: ignore[arg-type]
    assert frame.effective_id == effective_id
    assert frame.protocol_specific == effective_id - 0x100
    assert frame.command_index == effective_id - 0x100
    assert frame.descriptor == descriptor
    assert frame.payload == wire
    assert frame.wire_payload == descriptor + wire
    assert decode_frame(frame) == command
    assert decode_frame(TradeFrame.from_descriptor(descriptor, wire)) == command
    assert decode_frame(TradeFrame.from_wire_payload(descriptor + wire)) == command


def test_command_values_are_strictly_validated() -> None:
    with pytest.raises(CommandValidationError, match="sequence"):
        ConfirmTrade(0x10000)
    with pytest.raises(CommandValidationError, match="party_pa9"):
        SelectPokemon(0, bytes(0x148))
    with pytest.raises(CommandValidationError, match="preview"):
        SelectPokemon(0, bytes(0x158), 1)  # type: ignore[arg-type]
    with pytest.raises(CommandValidationError, match="reason"):
        CancelTrade(0, 0x80000000)


@pytest.mark.parametrize(
    ("effective_id", "wire", "match"),
    [
        (0x102, b"\xB8\x01\x00", "structure marker"),
        (0x102, b"\xB9\x02\x00", "fields"),
        (0x102, b"\xB9\x01\x00\x00", "trailing"),
        (0x102, b"\xB9\x01\x82\x00\x00\x01\x00", "sequence"),
        (0x101, b"\xB9\x03\x00\xBC\x00\x00", "expected 344"),
        (0x101, b"\xB9\x03\x00\xBC\x81\x58\x01" + bytes(0x158) + b"\x02", "Boolean"),
    ],
)
def test_decode_rejects_wrong_layout_and_trailing_bytes(
    effective_id: int, wire: bytes, match: str
) -> None:
    with pytest.raises(ArchiveError, match=match):
        decode_command(effective_id, wire)


def test_frame_descriptor_is_reliable_app_prefix_not_archive_data() -> None:
    payload = encode_command(ConfirmTrade(4))
    with pytest.raises(CommandValidationError, match="endpoint 1"):
        TradeFrame.from_descriptor(b"\x02\x02", payload)
    with pytest.raises(CommandValidationError, match="exactly 2"):
        TradeFrame.from_descriptor(b"\x01", payload)
    with pytest.raises(UnknownCommandError, match="command index"):
        TradeFrame.from_descriptor(b"\x01\x05", payload)
    with pytest.raises(CommandValidationError, match="shorter"):
        TradeFrame.from_wire_payload(b"\x01")
    with pytest.raises(UnknownCommandError, match="command ID"):
        decode_command(0x105, payload)
