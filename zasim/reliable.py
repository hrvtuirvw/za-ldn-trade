"""Pia 6.40 reliable-protocol wire primitives used by Z-A.

This module deliberately stops at protocol 10's payload boundary.  The Pia
compact-message header, station/session routing, and the title's application
descriptor are separate layers.

The layout below was checked against the Z-A 1.0.1 serializer/parser in
addition to the public Pia sliding-window documentation.  In this build byte
7 is a *destination bit count*.  The number of following BE32 bitmap words is
``ceil(destination_bit_count / 32)``; it is not itself a word count.  Keeping
that distinction matters for non-zero multicast headers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntFlag
from typing import Iterable


BASE_HEADER_SIZE = 8
BITMAP_WORD_SIZE = 4
ACK_MASK_SIZE = 16
# The header's BE16 field has a coarse parser ceiling below 0x590, while the
# sender/receiver impose the tighter transport ceiling below.  Every bitmap
# word consumes four bytes of that 0x580 fragment budget.
MAX_DECLARED_PAYLOAD_SIZE = 0x58F
MAX_FRAGMENT_BUDGET = 0x580
MAX_PAYLOAD_SIZE = MAX_FRAGMENT_BUDGET
MAX_DESTINATION_BITS = 23
SEQUENCE_MODULUS = 0x10000
DEFAULT_RECEIVE_WINDOW = ACK_MASK_SIZE * 8
DEFAULT_MAX_REASSEMBLED_SIZE = 64 * 1024
DEFAULT_MAX_REASSEMBLED_FRAGMENTS = 256


class ReliableError(ValueError):
    """Base class for malformed reliable-layer data."""


class ReliableFrameError(ReliableError):
    """A reliable frame is structurally invalid."""


class BulkAckError(ReliableError):
    """A bulk-acknowledgement payload is structurally invalid."""


class ReassemblyError(ReliableError):
    """Reliable application fragments cannot form a valid message."""


class SequenceGapError(ReassemblyError):
    """A sequence lies beyond the configured receive window."""


class ConflictingDuplicateError(ReassemblyError):
    """The same sequence id was observed with different contents."""


class FragmentationError(ReassemblyError):
    """Start/end flags do not describe a valid fragment sequence."""


class ReliableFlags(IntFlag):
    """Flags in byte 0 of a Pia reliable sliding-window frame."""

    APPLICATION_DATA = 0x01
    MESSAGE_START = 0x02
    MESSAGE_END = 0x04
    INITIALIZED = 0x08
    COMPRESSED = 0x10
    RESET = 0x20
    RESET_ACK = 0x40


KNOWN_FLAG_MASK = int(
    ReliableFlags.APPLICATION_DATA
    | ReliableFlags.MESSAGE_START
    | ReliableFlags.MESSAGE_END
    | ReliableFlags.INITIALIZED
    | ReliableFlags.COMPRESSED
    | ReliableFlags.RESET
    | ReliableFlags.RESET_ACK
)

# Short aliases are useful while comparing packet dumps with the public flag
# table, without importing any title-specific constants.
FLAG_APPLICATION_DATA = ReliableFlags.APPLICATION_DATA
FLAG_MESSAGE_START = ReliableFlags.MESSAGE_START
FLAG_MESSAGE_END = ReliableFlags.MESSAGE_END
FLAG_INITIALIZED = ReliableFlags.INITIALIZED
FLAG_COMPRESSED = ReliableFlags.COMPRESSED
FLAG_RESET = ReliableFlags.RESET
FLAG_RESET_ACK = ReliableFlags.RESET_ACK
FLAG_COMPLETE_MESSAGE = (
    ReliableFlags.APPLICATION_DATA
    | ReliableFlags.MESSAGE_START
    | ReliableFlags.MESSAGE_END
)


def _uint(value: int, bits: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    maximum = (1 << bits) - 1
    if not 0 <= value <= maximum:
        raise ValueError(f"{field} must be between 0 and {maximum}")
    return value


def _octets(value: bytes | bytearray | memoryview, field: str) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f"{field} must be bytes-like")
    return bytes(value)


def _coerce_flags(value: int | ReliableFlags) -> ReliableFlags:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("flags must be an integer or ReliableFlags")
    if not 0 <= value <= 0xFF:
        raise ValueError("flags must fit in one byte")
    unknown = value & ~KNOWN_FLAG_MASK
    if unknown:
        raise ReliableFrameError(f"unknown reliable flag bits 0x{unknown:02x}")
    return ReliableFlags(value)


def _bitmap_word_count(destination_bit_count: int) -> int:
    return (destination_bit_count + 31) // 32


@dataclass(frozen=True, slots=True)
class ReliableFrame:
    """One protocol-10 sliding-window frame.

    ``bitmap_words`` are numeric BE32 wire words.  Z-A rejects a destination
    bit count of 24 or greater, so its unicast header has no word and its
    multicast header has exactly one.  Unused high bits are preserved: the
    game parser does not provide enough evidence to canonicalize them.
    """

    flags: ReliableFlags | int
    sequence_id: int
    lowest_pending_ack: int
    payload: bytes = b""
    destination_bit_count: int = 0
    bitmap_words: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        flags = _coerce_flags(self.flags)
        sequence_id = _uint(self.sequence_id, 16, "sequence_id")
        lowest = _uint(self.lowest_pending_ack, 16, "lowest_pending_ack")
        payload = _octets(self.payload, "payload")
        bit_count = _uint(
            self.destination_bit_count, 8, "destination_bit_count"
        )
        if bit_count > MAX_DESTINATION_BITS:
            raise ReliableFrameError(
                f"Z-A accepts at most {MAX_DESTINATION_BITS} destination bits"
            )
        try:
            words = tuple(self.bitmap_words)
        except TypeError as exc:
            raise TypeError("bitmap_words must be an iterable of BE32 values") from exc
        expected = _bitmap_word_count(bit_count)
        if len(words) != expected:
            raise ReliableFrameError(
                f"destination_bit_count {bit_count} requires {expected} bitmap "
                f"word(s), got {len(words)}"
            )
        words = tuple(_uint(word, 32, "bitmap word") for word in words)
        maximum_payload = MAX_FRAGMENT_BUDGET - BITMAP_WORD_SIZE * expected
        if len(payload) > maximum_payload:
            raise ReliableFrameError(
                f"payload is {len(payload)} bytes; Z-A allows at most "
                f"{maximum_payload} with {expected} destination bitmap word(s)"
            )

        object.__setattr__(self, "flags", flags)
        object.__setattr__(self, "sequence_id", sequence_id)
        object.__setattr__(self, "lowest_pending_ack", lowest)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "destination_bit_count", bit_count)
        object.__setattr__(self, "bitmap_words", words)

    @property
    def payload_size(self) -> int:
        return len(self.payload)

    @property
    def bitmap_word_count(self) -> int:
        return len(self.bitmap_words)

    @property
    def header_size(self) -> int:
        return BASE_HEADER_SIZE + BITMAP_WORD_SIZE * self.bitmap_word_count

    @property
    def is_application_data(self) -> bool:
        return bool(self.flags & ReliableFlags.APPLICATION_DATA)

    @property
    def is_complete_message(self) -> bool:
        return (self.flags & FLAG_COMPLETE_MESSAGE) == FLAG_COMPLETE_MESSAGE

    def pack(self) -> bytes:
        header = bytearray()
        header.append(int(self.flags))
        header.extend(self.payload_size.to_bytes(2, "big"))
        header.extend(self.sequence_id.to_bytes(2, "big"))
        header.extend(self.lowest_pending_ack.to_bytes(2, "big"))
        header.append(self.destination_bit_count)
        for word in self.bitmap_words:
            header.extend(word.to_bytes(4, "big"))
        return bytes(header) + self.payload

    serialize = pack

    @classmethod
    def unpack(
        cls, data: bytes | bytearray | memoryview
    ) -> "ReliableFrame":
        raw = _octets(data, "data")
        if len(raw) < BASE_HEADER_SIZE:
            raise ReliableFrameError(
                f"reliable frame is {len(raw)} bytes; at least 8 are required"
            )

        flags = _coerce_flags(raw[0])
        payload_size = int.from_bytes(raw[1:3], "big")
        if payload_size > MAX_DECLARED_PAYLOAD_SIZE:
            raise ReliableFrameError(
                f"declared payload size 0x{payload_size:X} is rejected by Z-A "
                f"(coarse maximum 0x{MAX_DECLARED_PAYLOAD_SIZE:X})"
            )
        sequence_id = int.from_bytes(raw[3:5], "big")
        lowest = int.from_bytes(raw[5:7], "big")
        bit_count = raw[7]
        if bit_count > MAX_DESTINATION_BITS:
            raise ReliableFrameError(
                f"destination bit count {bit_count} is rejected by Z-A (maximum 23)"
            )
        word_count = _bitmap_word_count(bit_count)
        header_size = BASE_HEADER_SIZE + BITMAP_WORD_SIZE * word_count
        expected_size = header_size + payload_size
        if len(raw) != expected_size:
            relation = "truncated" if len(raw) < expected_size else "has trailing bytes"
            raise ReliableFrameError(
                f"reliable frame {relation}: header declares exactly "
                f"{expected_size} bytes, got {len(raw)}"
            )

        words = tuple(
            int.from_bytes(raw[offset : offset + BITMAP_WORD_SIZE], "big")
            for offset in range(BASE_HEADER_SIZE, header_size, BITMAP_WORD_SIZE)
        )
        return cls(
            flags=flags,
            sequence_id=sequence_id,
            lowest_pending_ack=lowest,
            payload=raw[header_size:],
            destination_bit_count=bit_count,
            bitmap_words=words,
        )

    parse = unpack


def build_reliable(
    sequence_id: int,
    lowest_pending_ack: int,
    payload: bytes | bytearray | memoryview,
    flags: int | ReliableFlags = FLAG_COMPLETE_MESSAGE,
    *,
    destination_bit_count: int = 0,
    bitmap_words: Iterable[int] = (),
) -> bytes:
    """Build one strict frame without adding Pia or title-specific headers."""

    return ReliableFrame(
        flags=flags,
        sequence_id=sequence_id,
        lowest_pending_ack=lowest_pending_ack,
        payload=_octets(payload, "payload"),
        destination_bit_count=destination_bit_count,
        bitmap_words=tuple(bitmap_words),
    ).pack()


def parse_reliable(data: bytes | bytearray | memoryview) -> ReliableFrame:
    """Parse one frame, rejecting both truncation and trailing data."""

    return ReliableFrame.unpack(data)


ZERO_ACK_MASK = bytes(ACK_MASK_SIZE)


def sequence_less_than(left: int, right: int) -> bool:
    """RFC-1982-style ordering in the unambiguous half of u16 space."""

    left = _uint(left, 16, "left sequence")
    right = _uint(right, 16, "right sequence")
    distance = (right - left) & 0xFFFF
    return 0 < distance < 0x8000


@dataclass(frozen=True, slots=True)
class BulkAckEntry:
    """One cumulative id and 128-bit selective acknowledgement mask.

    Captures show the id behaving as ``next_expected``: ids before it are
    cumulative acknowledgements.  Mask bit ``i`` acknowledges
    ``next_expected + 1 + i``.  Mask bits are LSB-first within each byte.
    """

    acknowledgement_id: int
    mask: bytes = ZERO_ACK_MASK

    def __post_init__(self) -> None:
        ack_id = _uint(self.acknowledgement_id, 16, "acknowledgement_id")
        mask = _octets(self.mask, "mask")
        if len(mask) != ACK_MASK_SIZE:
            raise BulkAckError(
                f"acknowledgement mask must be exactly {ACK_MASK_SIZE} bytes"
            )
        object.__setattr__(self, "acknowledgement_id", ack_id)
        object.__setattr__(self, "mask", mask)

    @property
    def next_expected(self) -> int:
        return self.acknowledgement_id

    def acknowledges(self, sequence_id: int) -> bool:
        sequence_id = _uint(sequence_id, 16, "sequence_id")
        if sequence_less_than(sequence_id, self.acknowledgement_id):
            return True
        index = (sequence_id - self.acknowledgement_id - 1) & 0xFFFF
        return index < 128 and bool(self.mask[index >> 3] & (1 << (index & 7)))

    @classmethod
    def from_received(
        cls, acknowledgement_id: int, received: Iterable[int]
    ) -> "BulkAckEntry":
        ack_id = _uint(acknowledgement_id, 16, "acknowledgement_id")
        mask = bytearray(ACK_MASK_SIZE)
        for sequence_id in received:
            sequence_id = _uint(sequence_id, 16, "received sequence")
            index = (sequence_id - ack_id - 1) & 0xFFFF
            if index >= 128:
                raise BulkAckError(
                    f"sequence 0x{sequence_id:04x} is outside the 128-bit "
                    f"selective window after 0x{ack_id:04x}"
                )
            mask[index >> 3] |= 1 << (index & 7)
        return cls(ack_id, bytes(mask))


@dataclass(frozen=True, slots=True)
class BulkAcknowledgement:
    """Observed Pia 6.40 control payload.

    The wire layout is ``ack_flags:u8, count:u8, entries...``.  Normal ACKs
    use flags zero; the recovered forced/resync path sets bit zero.  This byte
    is not the Compact Message protocol-specific id or a stream number.
    A Z-A unicast packet normally carries one entry; supporting the count
    explicitly keeps parsing lossless and prevents accepting truncated masks.
    """

    ack_flags: int
    entries: tuple[BulkAckEntry, ...]

    def __post_init__(self) -> None:
        ack_flags = _uint(self.ack_flags, 8, "ack_flags")
        if ack_flags & ~1:
            raise BulkAckError(
                f"unknown bulk acknowledgement flag bits 0x{ack_flags & ~1:02x}"
            )
        try:
            entries = tuple(self.entries)
        except TypeError as exc:
            raise TypeError("entries must be an iterable of BulkAckEntry") from exc
        if len(entries) >= 25:
            raise BulkAckError("Z-A bulk acknowledgements contain fewer than 25 entries")
        if any(not isinstance(entry, BulkAckEntry) for entry in entries):
            raise TypeError("every bulk acknowledgement entry must be BulkAckEntry")
        object.__setattr__(self, "ack_flags", ack_flags)
        object.__setattr__(self, "entries", entries)

    @property
    def stream_id(self) -> int:
        """Deprecated compatibility alias for the formerly misnamed field."""

        return self.ack_flags

    def pack(self) -> bytes:
        output = bytearray((self.ack_flags, len(self.entries)))
        for entry in self.entries:
            output.extend(entry.acknowledgement_id.to_bytes(2, "big"))
            output.extend(entry.mask)
        return bytes(output)

    serialize = pack

    @classmethod
    def unpack(
        cls, data: bytes | bytearray | memoryview
    ) -> "BulkAcknowledgement":
        raw = _octets(data, "data")
        if len(raw) < 2:
            raise BulkAckError("bulk acknowledgement needs ack flags and a count")
        count = raw[1]
        if count >= 25:
            raise BulkAckError("Z-A rejects bulk acknowledgement counts of 25 or more")
        entry_size = 2 + ACK_MASK_SIZE
        expected_size = 2 + count * entry_size
        if len(raw) != expected_size:
            relation = "truncated" if len(raw) < expected_size else "has trailing bytes"
            raise BulkAckError(
                f"bulk acknowledgement {relation}: count {count} requires exactly "
                f"{expected_size} bytes, got {len(raw)}"
            )
        entries = tuple(
            BulkAckEntry(
                int.from_bytes(raw[offset : offset + 2], "big"),
                raw[offset + 2 : offset + entry_size],
            )
            for offset in range(2, expected_size, entry_size)
        )
        return cls(raw[0], entries)

    parse = unpack


def build_bulk_ack(
    next_expected: int,
    mask: bytes | bytearray | memoryview = ZERO_ACK_MASK,
    ack_flags: int = 0,
    *,
    stream_id: int | None = None,
) -> bytes:
    """Build a one-entry Pia 6.40 bulk ACK.

    ``stream_id`` is a deprecated keyword alias retained for source
    compatibility; the byte is canonically ``ack_flags``.
    """

    if stream_id is not None:
        if ack_flags and ack_flags != stream_id:
            raise TypeError("ack_flags and stream_id alias disagree")
        ack_flags = stream_id

    return BulkAcknowledgement(
        ack_flags, (BulkAckEntry(next_expected, _octets(mask, "mask")),)
    ).pack()


def parse_bulk_ack(
    data: bytes | bytearray | memoryview,
) -> BulkAcknowledgement:
    return BulkAcknowledgement.unpack(data)


@dataclass(frozen=True, slots=True)
class ReassembledMessage:
    """One ordered application message; payload bytes remain opaque.

    In particular, this class does not strip a game's descriptor and does not
    inflate data marked ``COMPRESSED``.  ``flags`` is the union of fragment
    flags so callers can make that policy decision at the correct layer.
    """

    payload: bytes
    first_sequence: int
    last_sequence: int
    flags: ReliableFlags
    fragment_count: int

    @property
    def compressed(self) -> bool:
        return bool(self.flags & ReliableFlags.COMPRESSED)

    @property
    def initialized(self) -> bool:
        return bool(self.flags & ReliableFlags.INITIALIZED)

    def __bytes__(self) -> bytes:
        return self.payload


def _same_logical_fragment(first: ReliableFrame, second: ReliableFrame) -> bool:
    """ACK watermarks can advance (or arrive late) independently of payload.

    Keep every other wire field in the duplicate contract, including exact
    flags and destination bitmap. Reassembly does not consume the watermark.
    """
    return (
        first.sequence_id == second.sequence_id
        and first.flags == second.flags
        and first.payload == second.payload
        and first.destination_bit_count == second.destination_bit_count
        and first.bitmap_words == second.bitmap_words
    )


class ReliableReassembler:
    """Order and reassemble protocol-10 application-data frames.

    Future frames within ``receive_window`` are buffered across a gap.  Exact
    duplicates and stale frames are ignored; a conflicting duplicate raises.
    The ordering comparison is wrap-aware.  Control frames are returned to the
    caller as no application messages and do not consume application sequence
    ids, matching the observed bulk-ack path.

    If the negotiated receive base is known, pass it as ``next_sequence``.
    Otherwise the first application frame seeds the base, which is convenient
    for an already ordered capture but cannot recover an unseen earlier frame.
    """

    def __init__(
        self,
        next_sequence: int | None = None,
        *,
        receive_window: int = DEFAULT_RECEIVE_WINDOW,
        max_message_size: int = DEFAULT_MAX_REASSEMBLED_SIZE,
        max_fragments: int = DEFAULT_MAX_REASSEMBLED_FRAGMENTS,
    ) -> None:
        if next_sequence is not None:
            next_sequence = _uint(next_sequence, 16, "next_sequence")
        if isinstance(receive_window, bool) or not isinstance(receive_window, int):
            raise TypeError("receive_window must be an integer")
        if not 1 <= receive_window < 0x8000:
            raise ValueError("receive_window must be between 1 and 32767")
        if isinstance(max_message_size, bool) or not isinstance(max_message_size, int):
            raise TypeError("max_message_size must be an integer")
        if max_message_size < 1:
            raise ValueError("max_message_size must be positive")
        if isinstance(max_fragments, bool) or not isinstance(max_fragments, int):
            raise TypeError("max_fragments must be an integer")
        if not 1 <= max_fragments < SEQUENCE_MODULUS:
            raise ValueError("max_fragments must be between 1 and 65535")
        self._next_sequence = next_sequence
        self._receive_window = receive_window
        self._max_message_size = max_message_size
        self._max_fragments = max_fragments
        self._pending: dict[int, ReliableFrame] = {}
        self._parts: list[bytes] | None = None
        self._parts_size = 0
        self._part_flags = ReliableFlags(0)
        self._first_sequence: int | None = None
        self._fragment_count = 0

    @property
    def next_sequence(self) -> int | None:
        return self._next_sequence

    @property
    def pending_sequences(self) -> tuple[int, ...]:
        if self._next_sequence is None:
            return tuple(self._pending)
        return tuple(
            sorted(
                self._pending,
                key=lambda sequence: (sequence - self._next_sequence) & 0xFFFF,
            )
        )

    @property
    def has_partial_message(self) -> bool:
        return self._parts is not None

    def reset(self, next_sequence: int | None = None) -> None:
        if next_sequence is not None:
            next_sequence = _uint(next_sequence, 16, "next_sequence")
        self._next_sequence = next_sequence
        self._pending.clear()
        self._discard_partial()

    def _discard_partial(self) -> None:
        self._parts = None
        self._parts_size = 0
        self._part_flags = ReliableFlags(0)
        self._first_sequence = None
        self._fragment_count = 0

    def feed(self, frame: ReliableFrame) -> tuple[ReassembledMessage, ...]:
        if not isinstance(frame, ReliableFrame):
            raise TypeError("frame must be a ReliableFrame")
        if not frame.is_application_data:
            return ()

        sequence = frame.sequence_id
        if self._next_sequence is None:
            self._next_sequence = sequence
        assert self._next_sequence is not None
        distance = (sequence - self._next_sequence) & 0xFFFF

        if distance == 0:
            return self._consume_contiguous(frame)
        if distance >= 0x8000:
            return ()  # stale or already delivered
        if distance > self._receive_window:
            raise SequenceGapError(
                f"sequence 0x{sequence:04x} is {distance} places ahead of "
                f"0x{self._next_sequence:04x}; receive window is "
                f"{self._receive_window}"
            )

        existing = self._pending.get(sequence)
        if existing is None:
            self._pending[sequence] = frame
        elif not _same_logical_fragment(existing, frame):
            raise ConflictingDuplicateError(
                f"sequence 0x{sequence:04x} was buffered with different contents"
            )
        return ()

    push = feed

    def _consume_contiguous(
        self, first: ReliableFrame
    ) -> tuple[ReassembledMessage, ...]:
        output: list[ReassembledMessage] = []
        frame = first
        while True:
            completed = self._consume_fragment(frame)
            if completed is not None:
                output.append(completed)
            assert self._next_sequence is not None
            self._next_sequence = (self._next_sequence + 1) & 0xFFFF
            following = self._pending.pop(self._next_sequence, None)
            if following is None:
                break
            frame = following
        return tuple(output)

    def _consume_fragment(
        self, frame: ReliableFrame
    ) -> ReassembledMessage | None:
        start = bool(frame.flags & ReliableFlags.MESSAGE_START)
        end = bool(frame.flags & ReliableFlags.MESSAGE_END)

        if start:
            if self._parts is not None:
                raise FragmentationError(
                    f"sequence 0x{frame.sequence_id:04x} starts a new message "
                    "before the previous message ended"
                )
            self._parts = [frame.payload]
            self._parts_size = len(frame.payload)
            self._part_flags = ReliableFlags(frame.flags)
            self._first_sequence = frame.sequence_id
            self._fragment_count = 1
        else:
            if self._parts is None:
                raise FragmentationError(
                    f"sequence 0x{frame.sequence_id:04x} is a continuation "
                    "without a message-start fragment"
                )
            self._parts.append(frame.payload)
            self._parts_size += len(frame.payload)
            self._part_flags |= frame.flags
            self._fragment_count += 1

        if self._parts_size > self._max_message_size:
            actual_size = self._parts_size
            self._discard_partial()
            raise FragmentationError(
                "reassembled message exceeds the configured byte limit "
                f"({actual_size} > {self._max_message_size})"
            )
        if self._fragment_count > self._max_fragments:
            actual_fragments = self._fragment_count
            self._discard_partial()
            raise FragmentationError(
                "reassembled message exceeds the configured fragment limit "
                f"({actual_fragments} > {self._max_fragments})"
            )

        if not end:
            return None

        assert self._parts is not None
        assert self._first_sequence is not None
        message = ReassembledMessage(
            payload=b"".join(self._parts),
            first_sequence=self._first_sequence,
            last_sequence=frame.sequence_id,
            flags=self._part_flags,
            fragment_count=self._fragment_count,
        )
        self._discard_partial()
        return message


__all__ = [
    "ACK_MASK_SIZE",
    "BASE_HEADER_SIZE",
    "BITMAP_WORD_SIZE",
    "BulkAckEntry",
    "BulkAckError",
    "BulkAcknowledgement",
    "ConflictingDuplicateError",
    "DEFAULT_RECEIVE_WINDOW",
    "DEFAULT_MAX_REASSEMBLED_FRAGMENTS",
    "DEFAULT_MAX_REASSEMBLED_SIZE",
    "FLAG_APPLICATION_DATA",
    "FLAG_COMPLETE_MESSAGE",
    "FLAG_COMPRESSED",
    "FLAG_INITIALIZED",
    "FLAG_MESSAGE_END",
    "FLAG_MESSAGE_START",
    "FLAG_RESET",
    "FLAG_RESET_ACK",
    "FragmentationError",
    "MAX_DESTINATION_BITS",
    "MAX_DECLARED_PAYLOAD_SIZE",
    "MAX_FRAGMENT_BUDGET",
    "MAX_PAYLOAD_SIZE",
    "ReassembledMessage",
    "ReassemblyError",
    "ReliableError",
    "ReliableFlags",
    "ReliableFrame",
    "ReliableFrameError",
    "ReliableReassembler",
    "SequenceGapError",
    "ZERO_ACK_MASK",
    "build_bulk_ack",
    "build_reliable",
    "parse_bulk_ack",
    "parse_reliable",
    "sequence_less_than",
]
