"""Pokemon Legends: Z-A link-trade command archive primitives.

The title sends five application commands through endpoint 1.  The two-byte
endpoint descriptor (endpoint, command index) is prepended to the application
data carried by Pia Reliable; it is not part of the GFA compact archive and it
is not a Pia compact-message port/protocol-specific field.  :class:`TradeFrame`
keeps both views explicit.

The archive and command layouts in this module were recovered from the
``7222E13ECF6ADB32`` executable.  Session discovery and Pia's reliable-protocol
subheader are outside this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import TypeAlias


STRUCT_MARKER = 0xB9
BLOB_MARKER = 0xBC

TRADE_ENDPOINT = 1
# Compatibility aliases retained for early callers.  This byte is an
# application endpoint/channel, not Pia's compact-message ``port`` field.
TRADE_PORT = TRADE_ENDPOINT
TRADE_CHANNEL = TRADE_ENDPOINT

COMPATIBILITY_ROWS = 400
COMPATIBILITY_COLUMNS = 24
COMPATIBILITY_SIZE = COMPATIBILITY_ROWS * COMPATIBILITY_COLUMNS // 8
PARTY_PA9_SIZE = 0x158


class TradeProtocolError(ValueError):
    """Base class for invalid Z-A trade command data."""


class ArchiveError(TradeProtocolError):
    """The compact archive is truncated, malformed, or non-canonical."""


class CommandValidationError(TradeProtocolError):
    """A command object or command descriptor violates the recovered layout."""


class UnknownCommandError(TradeProtocolError):
    """The effective command ID is not part of the Z-A trade endpoint."""


def _bytes(value: object, name: str) -> bytes:
    try:
        return bytes(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be bytes-like") from exc


def _integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be from {minimum} through {maximum}")
    return value


def encode_varuint(value: int) -> bytes:
    """Encode an unsigned integer using GFA's canonical compact form."""

    value = _integer(value, "value", 0, 0xFFFFFFFFFFFFFFFF)
    if value <= 0x7F:
        return bytes((value,))
    if value <= 0xFF:
        return b"\x80" + value.to_bytes(1, "little")
    if value <= 0xFFFF:
        return b"\x81" + value.to_bytes(2, "little")
    if value <= 0xFFFFFFFF:
        return b"\x82" + value.to_bytes(4, "little")
    return b"\x83" + value.to_bytes(8, "little")


def encode_signed(value: int) -> bytes:
    """Encode a signed 32-bit integer using GFA's canonical compact form."""

    value = _integer(value, "value", -0x80000000, 0x7FFFFFFF)
    if -64 <= value <= 127:
        return bytes((value & 0xFF,))
    if -128 <= value <= 127:
        return b"\x84" + value.to_bytes(1, "little", signed=True)
    if -32768 <= value <= 32767:
        return b"\x85" + value.to_bytes(2, "little", signed=True)
    return b"\x86" + value.to_bytes(4, "little", signed=True)


def encode_blob(value: bytes | bytearray | memoryview) -> bytes:
    """Encode a raw byte array (``BC``, byte count, then bytes)."""

    raw = _bytes(value, "value")
    return bytes((BLOB_MARKER,)) + encode_varuint(len(raw)) + raw


def encode_struct(*fields: bytes | bytearray | memoryview) -> bytes:
    """Encode an object header (``B9`` and field count) plus encoded fields."""

    encoded = tuple(_bytes(field, "field") for field in fields)
    return bytes((STRUCT_MARKER,)) + encode_varuint(len(encoded)) + b"".join(encoded)


class ArchiveReader:
    """Strict cursor over one GFA compact archive payload.

    Integer encodings must use their shortest representation.  This removes
    ambiguities between primitive tags and the signed one-byte range and makes
    malformed captured data fail at the boundary where it was encountered.
    """

    def __init__(self, data: bytes | bytearray | memoryview) -> None:
        self.data = _bytes(data, "data")
        self.offset = 0

    @property
    def remaining(self) -> int:
        return len(self.data) - self.offset

    def _take(self, count: int, what: str) -> bytes:
        end = self.offset + count
        if end > len(self.data):
            raise ArchiveError(
                f"archive is truncated at offset {self.offset} while reading {what}"
            )
        result = self.data[self.offset:end]
        self.offset = end
        return result

    def _byte(self, what: str) -> int:
        return self._take(1, what)[0]

    def read_varuint(self) -> int:
        """Read a canonical compact unsigned integer."""

        start = self.offset
        tag = self._byte("an unsigned integer")
        if tag <= 0x7F:
            return tag
        widths = {0x80: 1, 0x81: 2, 0x82: 4, 0x83: 8}
        try:
            width = widths[tag]
        except KeyError as exc:
            raise ArchiveError(
                f"invalid unsigned-integer tag 0x{tag:02X} at offset {start}"
            ) from exc
        value = int.from_bytes(self._take(width, "an unsigned integer"), "little")
        lower_bounds = {1: 0x80, 2: 0x100, 4: 0x10000, 8: 0x100000000}
        if value < lower_bounds[width]:
            raise ArchiveError(
                f"non-canonical unsigned integer at offset {start}"
            )
        return value

    def read_signed(self) -> int:
        """Read a canonical compact signed 32-bit integer."""

        start = self.offset
        tag = self._byte("a signed integer")
        if tag <= 0x7F:
            return tag
        if tag >= 0xC0:
            return tag - 0x100
        widths = {0x84: 1, 0x85: 2, 0x86: 4}
        try:
            width = widths[tag]
        except KeyError as exc:
            raise ArchiveError(
                f"invalid signed-integer tag 0x{tag:02X} at offset {start}"
            ) from exc
        value = int.from_bytes(
            self._take(width, "a signed integer"), "little", signed=True
        )
        canonical = (
            (width == 1 and value < -64)
            or (width == 2 and not -128 <= value <= 127)
            or (width == 4 and not -32768 <= value <= 32767)
        )
        if not canonical:
            raise ArchiveError(f"non-canonical signed integer at offset {start}")
        return value

    def read_struct(self, expected_fields: int | None = None) -> int:
        """Read a ``B9`` object marker and return its field count."""

        marker_offset = self.offset
        marker = self._byte("a structure marker")
        if marker != STRUCT_MARKER:
            raise ArchiveError(
                f"expected structure marker 0x{STRUCT_MARKER:02X} at offset "
                f"{marker_offset}; got 0x{marker:02X}"
            )
        count = self.read_varuint()
        if expected_fields is not None and count != expected_fields:
            raise ArchiveError(
                f"structure at offset {marker_offset} has {count} fields; "
                f"expected {expected_fields}"
            )
        return count

    def read_blob(self, expected_size: int | None = None) -> bytes:
        """Read a ``BC`` byte array and optionally enforce its exact length."""

        marker_offset = self.offset
        marker = self._byte("a byte-array marker")
        if marker != BLOB_MARKER:
            raise ArchiveError(
                f"expected byte-array marker 0x{BLOB_MARKER:02X} at offset "
                f"{marker_offset}; got 0x{marker:02X}"
            )
        size = self.read_varuint()
        if expected_size is not None and size != expected_size:
            raise ArchiveError(
                f"byte array at offset {marker_offset} has {size} bytes; "
                f"expected {expected_size}"
            )
        return self._take(size, f"a {size}-byte byte array")

    def read_bool(self) -> bool:
        """Read the archive's raw one-byte Boolean representation."""

        offset = self.offset
        value = self._byte("a Boolean")
        if value not in (0, 1):
            raise ArchiveError(
                f"invalid Boolean value 0x{value:02X} at offset {offset}"
            )
        return bool(value)

    def finish(self) -> None:
        """Require the entire archive to have been consumed."""

        if self.remaining:
            raise ArchiveError(
                f"archive has {self.remaining} trailing byte(s) at offset {self.offset}"
            )


def decode_varuint(
    data: bytes | bytearray | memoryview, offset: int = 0
) -> tuple[int, int]:
    """Decode one compact unsigned value and return ``(value, next_offset)``."""

    raw = _bytes(data, "data")
    offset = _integer(offset, "offset", 0, len(raw))
    reader = ArchiveReader(raw[offset:])
    value = reader.read_varuint()
    return value, offset + reader.offset


def decode_signed(
    data: bytes | bytearray | memoryview, offset: int = 0
) -> tuple[int, int]:
    """Decode one compact signed value and return ``(value, next_offset)``."""

    raw = _bytes(data, "data")
    offset = _integer(offset, "offset", 0, len(raw))
    reader = ArchiveReader(raw[offset:])
    value = reader.read_signed()
    return value, offset + reader.offset


class CompatibilityMatrix:
    """The Ready command's 400 by 24, LSB-first compatibility bitmap.

    Rows are stored consecutively, with three bytes per row.  Within each byte
    column zero is the least-significant bit.  The game-specific meaning of
    individual rows and columns is intentionally not assigned here.
    """

    ROWS = COMPATIBILITY_ROWS
    COLUMNS = COMPATIBILITY_COLUMNS
    BYTE_SIZE = COMPATIBILITY_SIZE

    def __init__(self, data: bytes | bytearray | memoryview) -> None:
        raw = _bytes(data, "data")
        if len(raw) != self.BYTE_SIZE:
            raise ValueError(
                f"compatibility matrix must contain exactly 0x{self.BYTE_SIZE:X} "
                f"bytes; got 0x{len(raw):X}"
            )
        self._data = bytearray(raw)

    @classmethod
    def empty(cls) -> "CompatibilityMatrix":
        """Create a matrix with every bit cleared."""

        return cls(bytes(cls.BYTE_SIZE))

    @classmethod
    def full(cls) -> "CompatibilityMatrix":
        """Create a matrix with every bit set."""

        return cls(b"\xFF" * cls.BYTE_SIZE)

    @classmethod
    def from_bytes(
        cls, data: bytes | bytearray | memoryview
    ) -> "CompatibilityMatrix":
        return cls(data)

    @staticmethod
    def _bit(row: int, column: int) -> tuple[int, int]:
        row = _integer(row, "row", 0, COMPATIBILITY_ROWS - 1)
        column = _integer(column, "column", 0, COMPATIBILITY_COLUMNS - 1)
        bit = row * COMPATIBILITY_COLUMNS + column
        return bit >> 3, 1 << (bit & 7)

    def set(self, row: int, column: int, value: bool = True) -> "CompatibilityMatrix":
        """Set or clear one bit in place and return this matrix."""

        if not isinstance(value, bool):
            raise TypeError("value must be a bool")
        offset, mask = self._bit(row, column)
        if value:
            self._data[offset] |= mask
        else:
            self._data[offset] &= ~mask
        return self

    def test(self, row: int, column: int) -> bool:
        """Return whether one matrix bit is set."""

        offset, mask = self._bit(row, column)
        return bool(self._data[offset] & mask)

    def copy(self) -> "CompatibilityMatrix":
        return type(self)(self._data)

    def to_bytes(self) -> bytes:
        return bytes(self._data)

    def __bytes__(self) -> bytes:
        return self.to_bytes()

    def __len__(self) -> int:
        return self.BYTE_SIZE

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CompatibilityMatrix):
            return self._data == other._data
        return NotImplemented

    def __repr__(self) -> str:
        set_bits = sum(byte.bit_count() for byte in self._data)
        return f"CompatibilityMatrix(set_bits={set_bits}, size=0x{self.BYTE_SIZE:X})"


def _sequence(value: object) -> int:
    try:
        return _integer(value, "sequence", 0, 0xFFFF)
    except (TypeError, ValueError) as exc:
        raise CommandValidationError(str(exc)) from exc


@dataclass(frozen=True)
class Ready:
    """Save/compatibility synchronization command (effective ID ``0x100``)."""

    sequence: int
    matrix: CompatibilityMatrix

    def __post_init__(self) -> None:
        object.__setattr__(self, "sequence", _sequence(self.sequence))
        if not isinstance(self.matrix, CompatibilityMatrix):
            try:
                matrix = CompatibilityMatrix(self.matrix)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise CommandValidationError(str(exc)) from exc
            object.__setattr__(self, "matrix", matrix)

    @property
    def state(self) -> bytes:
        """Raw 0x4B0-byte state used by the nested archive structure."""

        return bytes(self.matrix)

    @property
    def compatibility(self) -> CompatibilityMatrix:
        return self.matrix


@dataclass(frozen=True)
class SelectPokemon:
    """Pokemon offer command (effective ID ``0x101``)."""

    sequence: int
    party_pa9: bytes
    preview: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "sequence", _sequence(self.sequence))
        raw = _bytes(self.party_pa9, "party_pa9")
        if len(raw) != PARTY_PA9_SIZE:
            raise CommandValidationError(
                f"party_pa9 must contain exactly 0x{PARTY_PA9_SIZE:X} bytes; "
                f"got 0x{len(raw):X}"
            )
        if not isinstance(self.preview, bool):
            raise CommandValidationError("preview must be a bool")
        object.__setattr__(self, "party_pa9", raw)

    @property
    def transient_or_clear(self) -> bool:
        """Behavior-neutral name for the still-unconfirmed Boolean semantics."""

        return self.preview


@dataclass(frozen=True)
class ConfirmTrade:
    """Offer-confirmation command (effective ID ``0x102``)."""

    sequence: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "sequence", _sequence(self.sequence))


@dataclass(frozen=True)
class CancelTrade:
    """Negotiation cancel/reset command (effective ID ``0x103``)."""

    sequence: int
    reason: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "sequence", _sequence(self.sequence))
        try:
            reason = _integer(self.reason, "reason", -0x80000000, 0x7FFFFFFF)
        except (TypeError, ValueError) as exc:
            raise CommandValidationError(str(exc)) from exc
        object.__setattr__(self, "reason", reason)


@dataclass(frozen=True)
class FinalAgreement:
    """Final two-party agreement command (effective ID ``0x104``)."""

    sequence: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "sequence", _sequence(self.sequence))


# Short aliases make state-machine code compact while retaining the recovered
# class names as the canonical public spelling.
Select = SelectPokemon
Confirm = ConfirmTrade
Cancel = CancelTrade
Final = FinalAgreement

TradeCommand: TypeAlias = Ready | SelectPokemon | ConfirmTrade | CancelTrade | FinalAgreement


class TradeCommandID(IntEnum):
    READY = 0x100
    SELECT_POKEMON = 0x101
    CONFIRM_TRADE = 0x102
    CANCEL_TRADE = 0x103
    FINAL_AGREEMENT = 0x104

    @property
    def command_index(self) -> int:
        return int(self) - int(TradeCommandID.READY)

    @property
    def protocol_specific(self) -> int:
        """Compatibility alias for the title command index.

        The byte is not Pia compact-message ``protocol_specific`` metadata;
        it lives in the title's two-byte Reliable application prefix.
        """

        return self.command_index


READY_ID = TradeCommandID.READY
SELECT_POKEMON_ID = TradeCommandID.SELECT_POKEMON
CONFIRM_TRADE_ID = TradeCommandID.CONFIRM_TRADE
CANCEL_TRADE_ID = TradeCommandID.CANCEL_TRADE
FINAL_AGREEMENT_ID = TradeCommandID.FINAL_AGREEMENT


_TYPE_TO_ID: dict[type[object], TradeCommandID] = {
    Ready: TradeCommandID.READY,
    SelectPokemon: TradeCommandID.SELECT_POKEMON,
    ConfirmTrade: TradeCommandID.CONFIRM_TRADE,
    CancelTrade: TradeCommandID.CANCEL_TRADE,
    FinalAgreement: TradeCommandID.FINAL_AGREEMENT,
}


def command_id(command: TradeCommand) -> TradeCommandID:
    """Return the recovered 0x100--0x104 effective ID for a command object."""

    try:
        return _TYPE_TO_ID[type(command)]
    except KeyError as exc:
        raise TypeError(f"unsupported trade command type: {type(command).__name__}") from exc


def encode_command(command: TradeCommand) -> bytes:
    """Encode one command's compact archive, without Pia transport metadata."""

    sequence = encode_varuint(command.sequence)
    if isinstance(command, Ready):
        nested = encode_struct(encode_blob(bytes(command.matrix)))
        return encode_struct(sequence, nested)
    if isinstance(command, SelectPokemon):
        return encode_struct(
            sequence,
            encode_blob(command.party_pa9),
            bytes((int(command.preview),)),
        )
    if isinstance(command, ConfirmTrade):
        return encode_struct(sequence)
    if isinstance(command, CancelTrade):
        return encode_struct(sequence, encode_signed(command.reason))
    if isinstance(command, FinalAgreement):
        return encode_struct(sequence)
    raise TypeError(f"unsupported trade command type: {type(command).__name__}")


def _id(value: TradeCommandID | int) -> TradeCommandID:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("command_id must be an integer or TradeCommandID")
    try:
        return TradeCommandID(value)
    except ValueError as exc:
        raise UnknownCommandError(f"unknown trade command ID 0x{value:X}") from exc


def _read_sequence(reader: ArchiveReader) -> int:
    value = reader.read_varuint()
    if value > 0xFFFF:
        raise ArchiveError(f"sequence {value} does not fit in an unsigned 16-bit field")
    return value


def decode_command(
    effective_id: TradeCommandID | int,
    payload: bytes | bytearray | memoryview,
) -> TradeCommand:
    """Decode one command archive and reject malformed or trailing data."""

    effective_id = _id(effective_id)
    reader = ArchiveReader(payload)

    if effective_id is TradeCommandID.READY:
        reader.read_struct(2)
        sequence = _read_sequence(reader)
        reader.read_struct(1)
        result: TradeCommand = Ready(
            sequence, CompatibilityMatrix(reader.read_blob(COMPATIBILITY_SIZE))
        )
    elif effective_id is TradeCommandID.SELECT_POKEMON:
        reader.read_struct(3)
        sequence = _read_sequence(reader)
        party_pa9 = reader.read_blob(PARTY_PA9_SIZE)
        result = SelectPokemon(sequence, party_pa9, reader.read_bool())
    elif effective_id is TradeCommandID.CONFIRM_TRADE:
        reader.read_struct(1)
        result = ConfirmTrade(_read_sequence(reader))
    elif effective_id is TradeCommandID.CANCEL_TRADE:
        reader.read_struct(2)
        result = CancelTrade(_read_sequence(reader), reader.read_signed())
    elif effective_id is TradeCommandID.FINAL_AGREEMENT:
        reader.read_struct(1)
        result = FinalAgreement(_read_sequence(reader))
    else:  # IntEnum exhaustiveness guard
        raise UnknownCommandError(f"unknown trade command ID 0x{int(effective_id):X}")

    reader.finish()
    return result


@dataclass(frozen=True)
class TradeFrame:
    """A command archive and its two-byte Reliable application prefix."""

    command_id: TradeCommandID
    payload: bytes
    endpoint: int = TRADE_ENDPOINT

    def __post_init__(self) -> None:
        try:
            parsed_id = _id(self.command_id)
        except (TypeError, UnknownCommandError) as exc:
            raise CommandValidationError(str(exc)) from exc
        if (
            not isinstance(self.endpoint, int)
            or isinstance(self.endpoint, bool)
            or self.endpoint != TRADE_ENDPOINT
        ):
            raise CommandValidationError(
                f"Z-A trade frames use application endpoint {TRADE_ENDPOINT}; "
                f"got {self.endpoint!r}"
            )
        object.__setattr__(self, "command_id", parsed_id)
        object.__setattr__(self, "payload", _bytes(self.payload, "payload"))

    @property
    def effective_id(self) -> int:
        return int(self.command_id)

    @property
    def protocol_specific(self) -> int:
        """Compatibility name for the application command-index byte."""

        return self.command_id.protocol_specific

    @property
    def command_index(self) -> int:
        return self.command_id.command_index

    @property
    def port(self) -> int:
        """Compatibility alias; this is not a Pia message-header port."""

        return self.endpoint

    @property
    def descriptor(self) -> bytes:
        """Return the ``endpoint,index`` Reliable application prefix."""

        return bytes((self.endpoint, self.command_index))

    @property
    def archive_payload(self) -> bytes:
        return self.payload

    @property
    def wire_payload(self) -> bytes:
        """Return the exact payload placed inside a Reliable data message."""

        return self.descriptor + self.payload

    @classmethod
    def from_descriptor(
        cls,
        descriptor: bytes | bytearray | memoryview,
        payload: bytes | bytearray | memoryview,
    ) -> "TradeFrame":
        """Build a frame from the two-byte endpoint descriptor and payload."""

        raw = _bytes(descriptor, "descriptor")
        if len(raw) != 2:
            raise CommandValidationError(
                f"trade descriptor must contain exactly 2 bytes; got {len(raw)}"
            )
        endpoint, command_index = raw
        if endpoint != TRADE_ENDPOINT:
            raise CommandValidationError(
                f"Z-A trade frames use application endpoint {TRADE_ENDPOINT}; "
                f"got {endpoint}"
            )
        try:
            effective_id = TradeCommandID(0x100 | command_index)
        except ValueError as exc:
            raise UnknownCommandError(
                f"unknown trade command index 0x{command_index:02X}"
            ) from exc
        return cls(effective_id, payload, endpoint)

    @classmethod
    def from_wire_payload(
        cls, payload: bytes | bytearray | memoryview
    ) -> "TradeFrame":
        """Split ``endpoint,index || archive`` from a Reliable app message."""

        raw = _bytes(payload, "payload")
        if len(raw) < 2:
            raise CommandValidationError(
                "trade Reliable application payload is shorter than its 2-byte prefix"
            )
        return cls.from_descriptor(raw[:2], raw[2:])


def encode_frame(command: TradeCommand) -> TradeFrame:
    """Encode a command and retain its Reliable application prefix."""

    return TradeFrame(command_id(command), encode_command(command))


def decode_frame(frame: TradeFrame) -> TradeCommand:
    """Decode a :class:`TradeFrame`, including complete archive validation."""

    if not isinstance(frame, TradeFrame):
        raise TypeError("frame must be a TradeFrame")
    return decode_command(frame.command_id, frame.payload)


__all__ = [
    "ArchiveError",
    "ArchiveReader",
    "BLOB_MARKER",
    "CANCEL_TRADE_ID",
    "COMPATIBILITY_COLUMNS",
    "COMPATIBILITY_ROWS",
    "COMPATIBILITY_SIZE",
    "CONFIRM_TRADE_ID",
    "Cancel",
    "CancelTrade",
    "CommandValidationError",
    "CompatibilityMatrix",
    "Confirm",
    "ConfirmTrade",
    "FINAL_AGREEMENT_ID",
    "Final",
    "FinalAgreement",
    "PARTY_PA9_SIZE",
    "READY_ID",
    "Ready",
    "SELECT_POKEMON_ID",
    "STRUCT_MARKER",
    "Select",
    "SelectPokemon",
    "TRADE_CHANNEL",
    "TRADE_ENDPOINT",
    "TRADE_PORT",
    "TradeCommand",
    "TradeCommandID",
    "TradeFrame",
    "TradeProtocolError",
    "UnknownCommandError",
    "command_id",
    "decode_command",
    "decode_frame",
    "decode_signed",
    "decode_varuint",
    "encode_blob",
    "encode_command",
    "encode_frame",
    "encode_signed",
    "encode_struct",
    "encode_varuint",
]
