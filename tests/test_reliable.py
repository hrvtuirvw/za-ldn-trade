from __future__ import annotations

import pytest

from zasim.reliable import (
    ACK_MASK_SIZE,
    BASE_HEADER_SIZE,
    BulkAckEntry,
    BulkAckError,
    BulkAcknowledgement,
    ConflictingDuplicateError,
    FLAG_COMPLETE_MESSAGE,
    FragmentationError,
    MAX_DECLARED_PAYLOAD_SIZE,
    MAX_FRAGMENT_BUDGET,
    ReliableFlags,
    ReliableFrame,
    ReliableFrameError,
    ReliableReassembler,
    SequenceGapError,
    build_bulk_ack,
    build_reliable,
    parse_bulk_ack,
    parse_reliable,
    sequence_less_than,
)
from zasim.trade_protocol import CompatibilityMatrix, Ready, encode_frame


def frame(
    sequence: int,
    flags: ReliableFlags | int,
    payload: bytes,
    *,
    lowest: int = 0,
) -> ReliableFrame:
    return ReliableFrame(flags, sequence, lowest, payload)


def test_unicast_header_exact_vector_and_roundtrip() -> None:
    value = ReliableFrame(
        ReliableFlags(FLAG_COMPLETE_MESSAGE | ReliableFlags.INITIALIZED),
        0xFFFE,
        0x1234,
        b"abc",
    )
    wire = value.pack()

    assert wire == bytes.fromhex("0f0003fffe123400616263")
    assert value.header_size == BASE_HEADER_SIZE
    assert parse_reliable(wire) == value
    assert value.serialize() == wire


@pytest.mark.parametrize("bit_count", [1, 2, 7, 23])
def test_destination_bit_count_uses_one_be32_word(bit_count: int) -> None:
    value = ReliableFrame(
        FLAG_COMPLETE_MESSAGE,
        0x0102,
        0x0304,
        b"Z",
        destination_bit_count=bit_count,
        bitmap_words=(0x81234567,),
    )
    wire = value.pack()

    assert wire[:8] == bytes.fromhex(f"07000101020304{bit_count:02x}")
    assert wire[8:12] == bytes.fromhex("81234567")
    assert wire[12:] == b"Z"
    assert value.header_size == 12
    assert value.bitmap_word_count == 1
    assert ReliableFrame.unpack(wire) == value


def test_destination_bitmap_shape_and_za_limit_are_strict() -> None:
    with pytest.raises(ReliableFrameError, match="requires 1 bitmap"):
        ReliableFrame(FLAG_COMPLETE_MESSAGE, 0, 0, b"", destination_bit_count=1)
    with pytest.raises(ReliableFrameError, match="requires 0 bitmap"):
        ReliableFrame(FLAG_COMPLETE_MESSAGE, 0, 0, b"", bitmap_words=(1,))
    with pytest.raises(ReliableFrameError, match="at most 23"):
        ReliableFrame(
            FLAG_COMPLETE_MESSAGE,
            0,
            0,
            b"",
            destination_bit_count=24,
            bitmap_words=(1,),
        )

    # The ROM parser rejects count >= 24 before trying to read a bitmap.
    with pytest.raises(ReliableFrameError, match="maximum 23"):
        ReliableFrame.unpack(bytes.fromhex("0700000000000018"))


def test_za_fragment_payload_budget_accounts_for_bitmap_bytes() -> None:
    unicast = ReliableFrame(FLAG_COMPLETE_MESSAGE, 0, 0, bytes(MAX_FRAGMENT_BUDGET))
    assert ReliableFrame.unpack(unicast.pack()) == unicast
    with pytest.raises(ReliableFrameError, match="allows at most 1408"):
        ReliableFrame(FLAG_COMPLETE_MESSAGE, 0, 0, bytes(MAX_FRAGMENT_BUDGET + 1))

    multicast_max = MAX_FRAGMENT_BUDGET - 4
    multicast = ReliableFrame(
        FLAG_COMPLETE_MESSAGE,
        0,
        0,
        bytes(multicast_max),
        destination_bit_count=1,
        bitmap_words=(1,),
    )
    assert ReliableFrame.unpack(multicast.pack()) == multicast
    with pytest.raises(ReliableFrameError, match="allows at most 1404"):
        ReliableFrame(
            FLAG_COMPLETE_MESSAGE,
            0,
            0,
            bytes(multicast_max + 1),
            destination_bit_count=1,
            bitmap_words=(1,),
        )

    coarse = bytearray(8)
    coarse[1:3] = (MAX_DECLARED_PAYLOAD_SIZE + 1).to_bytes(2, "big")
    with pytest.raises(ReliableFrameError, match="coarse maximum"):
        ReliableFrame.unpack(coarse)


def test_every_recovered_flag_roundtrips_but_unknown_bit_is_rejected() -> None:
    all_known = ReliableFlags(0x7F)
    value = ReliableFrame(all_known, 1, 1)
    assert ReliableFrame.unpack(value.pack()).flags == all_known

    with pytest.raises(ReliableFrameError, match="unknown reliable flag"):
        ReliableFrame.unpack(bytes.fromhex("8000000000000000"))
    with pytest.raises(ReliableFrameError, match="unknown reliable flag"):
        ReliableFrame(0x80, 0, 0)


@pytest.mark.parametrize(
    "wire",
    [
        b"",
        bytes(7),
        bytes.fromhex("0700020000000000aa"),  # declares two payload bytes
        bytes.fromhex("0700000000000000aa"),  # declares zero, has trailing data
        bytes.fromhex("0700000000000001"),  # bit count one, missing BE32 word
    ],
)
def test_frame_parser_rejects_truncation_and_trailing_data(wire: bytes) -> None:
    with pytest.raises(ReliableFrameError):
        ReliableFrame.unpack(wire)


def test_build_helpers_do_not_add_an_outer_pia_or_title_header() -> None:
    wire = build_reliable(4, 3, b"opaque", ReliableFlags.APPLICATION_DATA)
    parsed = parse_reliable(wire)
    assert parsed.sequence_id == 4
    assert parsed.lowest_pending_ack == 3
    assert parsed.payload == b"opaque"
    assert len(wire) == BASE_HEADER_SIZE + len(b"opaque")


def test_ready_sized_title_payload_fits_and_roundtrips() -> None:
    # The recovered title descriptor is part of this opaque application data;
    # it is not interpreted by the reliable codec.
    title_payload = encode_frame(
        Ready(0, CompatibilityMatrix.full())
    ).wire_payload
    assert len(title_payload) == 1211

    value = ReliableFrame(
        FLAG_COMPLETE_MESSAGE | ReliableFlags.INITIALIZED,
        0x4242,
        0x4242,
        title_payload,
    )
    wire = value.pack()
    assert len(wire) == BASE_HEADER_SIZE + 1211
    assert ReliableFrame.unpack(wire) == value


def test_bulk_ack_exact_observed_single_entry_layout() -> None:
    mask = bytearray(ACK_MASK_SIZE)
    mask[0] = 0x01
    mask[-1] = 0x80
    wire = build_bulk_ack(0xFFFE, bytes(mask), ack_flags=1)

    assert wire[:4] == bytes.fromhex("0101fffe")
    assert len(wire) == 20
    assert wire[4] == 0x01
    assert wire[-1] == 0x80
    decoded = parse_bulk_ack(wire)
    assert decoded == BulkAcknowledgement(1, (BulkAckEntry(0xFFFE, bytes(mask)),))
    assert decoded.ack_flags == 1
    assert decoded.stream_id == 1  # compatibility alias
    assert decoded.pack() == wire

    entry = decoded.entries[0]
    assert entry.next_expected == 0xFFFE
    assert entry.acknowledges(0xFFFD)  # cumulative region below next_expected
    assert not entry.acknowledges(0xFFFE)
    assert entry.acknowledges(0xFFFF)  # mask bit 0
    assert entry.acknowledges(0x007E)  # mask bit 127, across u16 wrap


def test_bulk_ack_multiple_entries_and_zero_count_roundtrip() -> None:
    value = BulkAcknowledgement(
        0,
        (
            BulkAckEntry(1),
            BulkAckEntry(0xFFFE, bytes(range(ACK_MASK_SIZE))),
        ),
    )
    wire = value.pack()
    assert wire[:2] == b"\x00\x02"
    assert len(wire) == 2 + 2 * 18
    assert BulkAcknowledgement.unpack(wire) == value

    empty = BulkAcknowledgement(0, ())
    assert BulkAcknowledgement.unpack(b"\x00\x00") == empty

    with pytest.raises(BulkAckError, match="unknown"):
        BulkAcknowledgement(2, ())
    with pytest.raises(BulkAckError, match="fewer than 25"):
        BulkAcknowledgement(0, (BulkAckEntry(0),) * 25)


@pytest.mark.parametrize(
    "wire",
    [
        b"",
        b"\0",
        b"\0\1",  # one entry declared but absent
        b"\0\0\0",  # zero entries plus trailing data
        b"\0\1\0\1" + bytes(15),  # one-byte-short mask
        b"\0\1\0\1" + bytes(17),  # one trailing byte
    ],
)
def test_bulk_ack_parser_rejects_wrong_exact_length(wire: bytes) -> None:
    with pytest.raises(BulkAckError):
        parse_bulk_ack(wire)


def test_selective_ack_builder_uses_lsb_first_bits_and_wraps() -> None:
    entry = BulkAckEntry.from_received(0xFFFF, [0x0000, 0x0001, 0x007F])
    assert entry.mask[0] == 0b00000011
    assert entry.mask[-1] == 0x80
    assert entry.acknowledges(0)
    assert entry.acknowledges(1)
    assert entry.acknowledges(0x7F)

    with pytest.raises(BulkAckError, match="outside"):
        BulkAckEntry.from_received(0, [129])


def test_u16_sequence_comparison_wraparound_and_half_range() -> None:
    assert sequence_less_than(0xFFFF, 0)
    assert sequence_less_than(0xFFFE, 1)
    assert not sequence_less_than(1, 0xFFFE)
    assert not sequence_less_than(4, 4)
    assert not sequence_less_than(0, 0x8000)  # deliberately ambiguous


def test_single_frame_reassembly_and_control_frame_ignored() -> None:
    reassembler = ReliableReassembler(10)
    control = frame(99, ReliableFlags.RESET_ACK, b"control")
    assert reassembler.feed(control) == ()
    assert reassembler.next_sequence == 10

    messages = reassembler.feed(frame(10, FLAG_COMPLETE_MESSAGE, b"whole"))
    assert len(messages) == 1
    assert bytes(messages[0]) == b"whole"
    assert messages[0].first_sequence == 10
    assert messages[0].last_sequence == 10
    assert messages[0].fragment_count == 1
    assert reassembler.next_sequence == 11


def test_multipart_reassembly_buffers_gap_and_drains_across_wrap() -> None:
    reassembler = ReliableReassembler(0xFFFE)
    start = frame(
        0xFFFE,
        ReliableFlags.APPLICATION_DATA | ReliableFlags.MESSAGE_START,
        b"A",
    )
    second = frame(0xFFFF, ReliableFlags.APPLICATION_DATA, b"B")
    third = frame(0, ReliableFlags.APPLICATION_DATA, b"C")
    end = frame(
        1,
        ReliableFlags.APPLICATION_DATA
        | ReliableFlags.MESSAGE_END
        | ReliableFlags.COMPRESSED,
        b"D",
    )

    assert reassembler.feed(second) == ()
    assert reassembler.feed(second) == ()  # exact buffered duplicate
    assert reassembler.pending_sequences == (0xFFFF,)
    assert reassembler.feed(end) == ()
    assert reassembler.feed(start) == ()  # drains start and buffered second
    assert reassembler.next_sequence == 0
    assert reassembler.has_partial_message

    messages = reassembler.feed(third)  # drains third and buffered end
    assert len(messages) == 1
    message = messages[0]
    assert message.payload == b"ABCD"
    assert (message.first_sequence, message.last_sequence) == (0xFFFE, 1)
    assert message.fragment_count == 4
    assert message.compressed
    assert reassembler.next_sequence == 2
    assert reassembler.pending_sequences == ()

    # A retransmit from the already-delivered range is stale and idempotent.
    assert reassembler.feed(second) == ()
    assert reassembler.next_sequence == 2


def test_buffered_conflicting_duplicate_is_rejected() -> None:
    reassembler = ReliableReassembler(5)
    assert reassembler.feed(frame(6, FLAG_COMPLETE_MESSAGE, b"one")) == ()
    with pytest.raises(ConflictingDuplicateError, match="different contents"):
        reassembler.feed(frame(6, FLAG_COMPLETE_MESSAGE, b"two"))


def test_gap_beyond_selective_ack_window_is_rejected() -> None:
    reassembler = ReliableReassembler(100, receive_window=128)
    assert reassembler.feed(frame(228, FLAG_COMPLETE_MESSAGE, b"edge")) == ()
    with pytest.raises(SequenceGapError, match="129 places"):
        reassembler.feed(frame(229, FLAG_COMPLETE_MESSAGE, b"outside"))


def test_fragment_flag_errors_are_not_silently_repaired() -> None:
    reassembler = ReliableReassembler(1)
    with pytest.raises(FragmentationError, match="without a message-start"):
        reassembler.feed(
            frame(
                1,
                ReliableFlags.APPLICATION_DATA | ReliableFlags.MESSAGE_END,
                b"tail",
            )
        )

    reassembler.reset(1)
    assert reassembler.feed(
        frame(
            1,
            ReliableFlags.APPLICATION_DATA | ReliableFlags.MESSAGE_START,
            b"first",
        )
    ) == ()
    with pytest.raises(FragmentationError, match="before the previous"):
        reassembler.feed(
            frame(
                2,
                ReliableFlags.APPLICATION_DATA | ReliableFlags.MESSAGE_START,
                b"replacement",
            )
        )


def test_reassembler_rejects_unbounded_partial_messages() -> None:
    byte_limited = ReliableReassembler(1, max_message_size=3)
    assert byte_limited.feed(
        frame(
            1,
            ReliableFlags.APPLICATION_DATA | ReliableFlags.MESSAGE_START,
            b"ab",
        )
    ) == ()
    with pytest.raises(FragmentationError, match="byte limit"):
        byte_limited.feed(frame(2, ReliableFlags.APPLICATION_DATA, b"cd"))
    assert not byte_limited.has_partial_message

    fragment_limited = ReliableReassembler(10, max_fragments=2)
    assert fragment_limited.feed(
        frame(
            10,
            ReliableFlags.APPLICATION_DATA | ReliableFlags.MESSAGE_START,
            b"a",
        )
    ) == ()
    assert fragment_limited.feed(
        frame(11, ReliableFlags.APPLICATION_DATA, b"b")
    ) == ()
    with pytest.raises(FragmentationError, match="fragment limit"):
        fragment_limited.feed(frame(12, ReliableFlags.APPLICATION_DATA, b"c"))
    assert not fragment_limited.has_partial_message

def test_reassembler_can_seed_from_first_ordered_capture_frame() -> None:
    reassembler = ReliableReassembler()
    messages = reassembler.push(frame(0xCAFE, FLAG_COMPLETE_MESSAGE, b"capture"))
    assert messages[0].payload == b"capture"
    assert reassembler.next_sequence == 0xCAFF
