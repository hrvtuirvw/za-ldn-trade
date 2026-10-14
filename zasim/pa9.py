"""Pokémon Legends: Z-A ``.pa9`` codec.

This module is a small, dependency-free implementation of the PA9 container
operations used by PKHeX: the 0x148-byte stored form, the 0x158-byte party
form, Gen 8/9 block shuffling, the 32-bit LCRNG XOR stream, and the additive
16-bit checksum.  A :class:`PA9` instance always holds decrypted data in
canonical block order; encryption is applied only while exporting bytes.

The field layout and algorithms follow PKHeX.Core's ``PA9`` and
``PokeCrypto`` implementations.  This module intentionally does not contain
game dumps, keys, or captured Pokémon data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from struct import pack_into, unpack_from
from typing import Optional, Union


STORED_SIZE = 0x148
"""Size, in bytes, of a stored PA9 record."""

PARTY_SIZE = 0x158
"""Size, in bytes, of a PA9 record including party stats."""

PARTY_LEVEL_OFFSET = 0x148
PARTY_HP_MAX_OFFSET = 0x14A

HEADER_SIZE = 8
BLOCK_SIZE = 0x50
BLOCK_COUNT = 4
BLOCK_REGION_SIZE = BLOCK_SIZE * BLOCK_COUNT

NICKNAME_OFFSET = 0x58
NICKNAME_SIZE = 26
MAX_NICKNAME_CODE_UNITS = 12
TRAINER_NAME_OFFSET = 0xF8
TRAINER_NAME_SIZE = 26
MAX_TRAINER_NAME_CODE_UNITS = 12

_LCRNG_MULTIPLIER = 0x41C64E6D
_LCRNG_INCREMENT = 0x6073
_UINT32_MASK = 0xFFFFFFFF

# PKHeX's table has these 24 permutations followed by a duplicate of entries
# 0..7.  Reducing the five-bit shuffle value modulo 24 is equivalent.
_BLOCK_ORDERS = (
    (0, 1, 2, 3),
    (0, 1, 3, 2),
    (0, 2, 1, 3),
    (0, 3, 1, 2),
    (0, 2, 3, 1),
    (0, 3, 2, 1),
    (1, 0, 2, 3),
    (1, 0, 3, 2),
    (2, 0, 1, 3),
    (3, 0, 1, 2),
    (2, 0, 3, 1),
    (3, 0, 2, 1),
    (1, 2, 0, 3),
    (1, 3, 0, 2),
    (2, 1, 0, 3),
    (3, 1, 0, 2),
    (2, 3, 0, 1),
    (3, 2, 0, 1),
    (1, 2, 3, 0),
    (1, 3, 2, 0),
    (2, 1, 3, 0),
    (3, 1, 2, 0),
    (2, 3, 1, 0),
    (3, 2, 1, 0),
)

BytesLike = Union[bytes, bytearray, memoryview]


class PA9Error(ValueError):
    """Base class for malformed or invalid PA9 data."""


class PA9SizeError(PA9Error):
    """Raised when PA9 data has an unsupported length."""


class PA9ValidationError(PA9Error):
    """Raised when a decrypted PA9 record fails structural validation."""


class PA9SanityError(PA9ValidationError):
    """Raised when the PA9 sanity word is non-zero."""


class PA9ChecksumError(PA9ValidationError):
    """Raised when the stored and calculated PA9 checksums differ."""


def _as_bytes(data: BytesLike, description: str = "data") -> bytes:
    try:
        return memoryview(data).tobytes()
    except TypeError as exc:
        raise TypeError(f"{description} must be a bytes-like object") from exc


def _record_bytes(data: BytesLike) -> bytes:
    raw = _as_bytes(data, "PA9 data")
    if len(raw) not in (STORED_SIZE, PARTY_SIZE):
        raise PA9SizeError(
            "PA9 data must be exactly "
            f"0x{STORED_SIZE:X} ({STORED_SIZE}) or "
            f"0x{PARTY_SIZE:X} ({PARTY_SIZE}) bytes; got {len(raw)}"
        )
    return raw


def _uint(value: int, bits: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field} must be an integer")
    maximum = (1 << bits) - 1
    if not 0 <= value <= maximum:
        raise ValueError(f"{field} must be in range 0..0x{maximum:X}; got {value!r}")
    return value


def _boolean(value: bool, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a bool")
    return value


def _integer_property(
    offset: int, bits: int, field: str, doc: str
) -> property:
    """Create a small typed integer property for a fixed PA9 offset."""

    getter_name = f"_get_u{bits}"
    setter_name = f"_set_u{bits}"

    def get_value(self: "PA9") -> int:
        return getattr(self, getter_name)(offset)

    def set_value(self: "PA9", value: int) -> None:
        getattr(self, setter_name)(offset, value, field)

    return property(get_value, set_value, doc=doc)


def _masked_byte_property(
    offset: int, mask: int, shift: int, field: str, doc: str
) -> property:
    """Create a byte bit-field property while preserving unrelated bits."""

    maximum = mask >> shift

    def get_value(self: "PA9") -> int:
        return (self._get_u8(offset) & mask) >> shift

    def set_value(self: "PA9", value: int) -> None:
        checked = _uint(value, maximum.bit_length(), field)
        if checked > maximum:
            raise ValueError(f"{field} must be in range 0..{maximum}; got {value!r}")
        current = self._get_u8(offset)
        self._set_u8(offset, (current & ~mask) | (checked << shift), field)

    return property(get_value, set_value, doc=doc)


def _boolean_bit_property(offset: int, mask: int, field: str, doc: str) -> property:
    """Create a Boolean byte bit-field property."""

    def get_value(self: "PA9") -> bool:
        return bool(self._get_u8(offset) & mask)

    def set_value(self: "PA9", value: bool) -> None:
        checked = _boolean(value, field)
        current = self._get_u8(offset)
        self._set_u8(offset, (current & ~mask) | (mask if checked else 0), field)

    return property(get_value, set_value, doc=doc)


def _iv_property(shift: int, field: str, doc: str) -> property:
    """Create one five-bit IV accessor in the packed IV32 word."""

    mask = 0x1F << shift

    def get_value(self: "PA9") -> int:
        return (self.iv32 >> shift) & 0x1F

    def set_value(self: "PA9", value: int) -> None:
        checked = _uint(value, 5, field)
        self.iv32 = (self.iv32 & ~mask) | (checked << shift)

    return property(get_value, set_value, doc=doc)


def _iv_flag_property(mask: int, field: str, doc: str) -> property:
    """Create one Boolean flag stored in the packed IV32 word."""

    def get_value(self: "PA9") -> bool:
        return bool(self.iv32 & mask)

    def set_value(self: "PA9", value: bool) -> None:
        checked = _boolean(value, field)
        self.iv32 = (self.iv32 & ~mask) | (mask if checked else 0)

    return property(get_value, set_value, doc=doc)


def _party_u16_property(offset: int, field: str, doc: str) -> property:
    """Create a cached party-stat property unavailable on stored records."""

    def get_value(self: "PA9") -> int:
        return self._get_u16(offset) if self.is_party else 0

    def set_value(self: "PA9", value: int) -> None:
        self._require_party(field)
        self._set_u16(offset, value, field)

    return property(get_value, set_value, doc=doc)


def _shuffle_value(encryption_constant: int) -> int:
    return (encryption_constant >> 13) & 31


def _crypt_region(buffer: bytearray, start: int, end: int, seed: int) -> None:
    for offset in range(start, end, 2):
        seed = (_LCRNG_MULTIPLIER * seed + _LCRNG_INCREMENT) & _UINT32_MASK
        word = buffer[offset] | (buffer[offset + 1] << 8)
        word ^= seed >> 16
        buffer[offset] = word & 0xFF
        buffer[offset + 1] = word >> 8


def crypt_lcrng(data: BytesLike, seed: int) -> bytes:
    """XOR *data* with the PA9 LCRNG stream and return new bytes.

    Encryption and decryption are the same operation.  The stream advances
    once per little-endian 16-bit word using ``seed = seed * 0x41C64E6D +
    0x6073`` modulo 2**32, then XORs the word with ``seed >> 16``.

    Args:
        data: An even-length bytes-like object.
        seed: Unsigned 32-bit initial seed (the PA9 EC for record data).

    Raises:
        TypeError: If *data* is not bytes-like or *seed* is not an integer.
        ValueError: If *data* has odd length or *seed* is outside uint32.
    """

    raw = _as_bytes(data)
    seed = _uint(seed, 32, "seed")
    if len(raw) & 1:
        raise ValueError(f"LCRNG data length must be even; got {len(raw)}")
    result = bytearray(raw)
    _crypt_region(result, 0, len(result), seed)
    return bytes(result)


def crypt_array(data: BytesLike, seed: int) -> bytes:
    """Compatibility alias for :func:`crypt_lcrng`."""

    return crypt_lcrng(data, seed)


def _block_bytes(data: BytesLike) -> bytes:
    raw = _as_bytes(data, "block data")
    if len(raw) != BLOCK_REGION_SIZE:
        raise PA9SizeError(
            f"block data must be exactly 0x{BLOCK_REGION_SIZE:X} "
            f"({BLOCK_REGION_SIZE}) bytes; got {len(raw)}"
        )
    return raw


def _reorder_blocks(data: bytes, order: tuple[int, int, int, int]) -> bytes:
    return b"".join(
        data[index * BLOCK_SIZE : (index + 1) * BLOCK_SIZE] for index in order
    )


def shuffle_blocks(data: BytesLike, shuffle_value: int) -> bytes:
    """Put canonical PA9 blocks into their encrypted storage order.

    Args:
        data: Exactly four canonical 0x50-byte blocks (0x140 bytes total).
        shuffle_value: The five-bit value ``(EC >> 13) & 31``.

    Returns:
        A new 0x140-byte object in shuffled order.  Passing the result to
        :func:`unshuffle_blocks` with the same value restores *data*.
    """

    raw = _block_bytes(data)
    value = _uint(shuffle_value, 5, "shuffle_value")
    decrypt_order = _BLOCK_ORDERS[value % 24]
    inverse = [0] * BLOCK_COUNT
    for canonical_slot, shuffled_slot in enumerate(decrypt_order):
        inverse[shuffled_slot] = canonical_slot
    return _reorder_blocks(raw, tuple(inverse))


def unshuffle_blocks(data: BytesLike, shuffle_value: int) -> bytes:
    """Restore four shuffled PA9 blocks to canonical field order.

    Args:
        data: Exactly four shuffled 0x50-byte blocks (0x140 bytes total).
        shuffle_value: The five-bit value ``(EC >> 13) & 31``.
    """

    raw = _block_bytes(data)
    value = _uint(shuffle_value, 5, "shuffle_value")
    return _reorder_blocks(raw, _BLOCK_ORDERS[value % 24])


def calculate_checksum(data: BytesLike) -> int:
    """Calculate the PA9 additive checksum from decrypted canonical data.

    Only the stored block region, offsets 0x08 through 0x147 inclusive, is
    checksummed.  Party stats are deliberately excluded.
    """

    raw = _record_bytes(data)
    total = 0
    for offset in range(HEADER_SIZE, STORED_SIZE, 2):
        total += raw[offset] | (raw[offset + 1] << 8)
    return total & 0xFFFF


def is_encrypted_pa9(data: BytesLike) -> bool:
    """Return PKHeX's heuristic encryption status for a PA9 record.

    In canonical data, the last UTF-16 code unit of both the nickname and
    original-trainer buffers is a NUL terminator.  PKHeX treats the record as
    encrypted if either code unit is non-zero.  For intentionally malformed
    full-length strings, callers can override detection with ``encrypted=``
    when constructing :class:`PA9`.
    """

    raw = _record_bytes(data)
    return raw[0x70:0x72] != b"\0\0" or raw[0x110:0x112] != b"\0\0"


def decrypt_pa9(data: BytesLike) -> bytes:
    """Unconditionally decrypt and unshuffle a stored or party PA9 record.

    The returned object has the same length as *data*.  This function mirrors
    PKHeX's unconditional ``Decrypt8`` primitive; use
    :func:`decrypt_if_encrypted` if the input state is unknown.
    """

    raw = _record_bytes(data)
    result = bytearray(raw)
    encryption_constant = unpack_from("<I", result, 0)[0]
    _crypt_region(result, HEADER_SIZE, STORED_SIZE, encryption_constant)
    if len(result) == PARTY_SIZE:
        # PKHeX starts a fresh EC-seeded stream for the party-stat tail.
        _crypt_region(result, STORED_SIZE, PARTY_SIZE, encryption_constant)
    result[HEADER_SIZE:STORED_SIZE] = unshuffle_blocks(
        result[HEADER_SIZE:STORED_SIZE], _shuffle_value(encryption_constant)
    )
    return bytes(result)


def decrypt_if_encrypted(data: BytesLike) -> bytes:
    """Decrypt *data* only when :func:`is_encrypted_pa9` says it is encrypted."""

    raw = _record_bytes(data)
    return decrypt_pa9(raw) if is_encrypted_pa9(raw) else raw


def encrypt_pa9(data: BytesLike, *, update_checksum: bool = True) -> bytes:
    """Encrypt canonical PA9 data, preserving its stored/party length.

    Args:
        data: Decrypted PA9 bytes in canonical block order.
        update_checksum: Recalculate and write the checksum before encryption.
    """

    if not isinstance(update_checksum, bool):
        raise TypeError("update_checksum must be a bool")
    raw = _record_bytes(data)
    result = bytearray(raw)
    if update_checksum:
        pack_into("<H", result, 6, calculate_checksum(result))
    encryption_constant = unpack_from("<I", result, 0)[0]
    result[HEADER_SIZE:STORED_SIZE] = shuffle_blocks(
        result[HEADER_SIZE:STORED_SIZE], _shuffle_value(encryption_constant)
    )
    _crypt_region(result, HEADER_SIZE, STORED_SIZE, encryption_constant)
    if len(result) == PARTY_SIZE:
        _crypt_region(result, STORED_SIZE, PARTY_SIZE, encryption_constant)
    return bytes(result)


class PA9:
    """Mutable decoded PA9 record with basic field accessors.

    Args:
        data: A 0x148-byte stored record or 0x158-byte party record.  ``None``
            creates an empty, checksum-valid party record.
        encrypted: ``True`` to force decryption, ``False`` to treat *data* as
            canonical, or ``None`` to use :func:`is_encrypted_pa9`.
        validate: Raise a detailed exception for a non-zero sanity word or bad
            checksum after decoding.

    The in-memory bytes are always decrypted and in canonical block order.
    Field edits make the checksum stale until :meth:`update_checksum`,
    :meth:`to_bytes`, or :meth:`save` is called with checksum updates enabled.
    """

    STORED_SIZE = STORED_SIZE
    PARTY_SIZE = PARTY_SIZE

    def __init__(
        self,
        data: Optional[BytesLike] = None,
        *,
        encrypted: Optional[bool] = None,
        validate: bool = True,
    ) -> None:
        if encrypted is not None and not isinstance(encrypted, bool):
            raise TypeError("encrypted must be True, False, or None")
        if not isinstance(validate, bool):
            raise TypeError("validate must be a bool")
        if data is None:
            if encrypted:
                raise ValueError("encrypted=True requires PA9 input data")
            empty = bytearray(PARTY_SIZE)
            # PKHeX initializes a new PA9 with no displayed ribbon selected.
            # AffixedRibbon is an sbyte, so its ``None`` value (-1) is stored
            # as 0xFF rather than the zero value (ribbon index 0).
            empty[0xD4] = 0xFF
            pack_into("<H", empty, 6, calculate_checksum(empty))
            raw = bytes(empty)
            detected_encrypted = False
        else:
            raw = _record_bytes(data)
            detected_encrypted = is_encrypted_pa9(raw) if encrypted is None else encrypted

        self.was_encrypted = bool(detected_encrypted)
        self._data = bytearray(decrypt_pa9(raw) if detected_encrypted else raw)
        if validate:
            self.validate()

    @classmethod
    def from_file(
        cls,
        path: Union[str, Path],
        *,
        encrypted: Optional[bool] = None,
        validate: bool = True,
    ) -> "PA9":
        """Read a stored or party PA9 record from *path*."""

        file_path = Path(path)
        raw = file_path.read_bytes()
        try:
            return cls(raw, encrypted=encrypted, validate=validate)
        except PA9SizeError as exc:
            raise PA9SizeError(f"{file_path}: {exc}") from exc

    @property
    def data(self) -> bytes:
        """Return a snapshot of the decrypted canonical record bytes."""

        return bytes(self._data)

    @property
    def size(self) -> int:
        """Current record size (0x148 stored or 0x158 party)."""

        return len(self._data)

    @property
    def is_party(self) -> bool:
        """Whether this record includes the 16-byte party-stat tail."""

        return len(self._data) == PARTY_SIZE

    @property
    def stat_level(self) -> int:
        """Cached party level, or zero for a stored-only record."""

        return self._data[PARTY_LEVEL_OFFSET] if self.is_party else 0

    @stat_level.setter
    def stat_level(self, value: int) -> None:
        self._require_party("stat_level")
        self._set_u8(PARTY_LEVEL_OFFSET, value, "stat_level")

    @property
    def stat_hp_max(self) -> int:
        """Cached maximum HP, or zero for a stored-only record."""

        return self._get_u16(PARTY_HP_MAX_OFFSET) if self.is_party else 0

    @stat_hp_max.setter
    def stat_hp_max(self, value: int) -> None:
        self._require_party("stat_hp_max")
        self._set_u16(PARTY_HP_MAX_OFFSET, value, "stat_hp_max")

    stat_atk = _party_u16_property(0x14C, "stat_atk", "Cached party Attack stat.")
    stat_def = _party_u16_property(0x14E, "stat_def", "Cached party Defense stat.")
    stat_spe = _party_u16_property(0x150, "stat_spe", "Cached party Speed stat.")
    stat_spa = _party_u16_property(
        0x152, "stat_spa", "Cached party Special Attack stat."
    )
    stat_spd = _party_u16_property(
        0x154, "stat_spd", "Cached party Special Defense stat."
    )

    @property
    def party_stats_present(self) -> bool:
        """Whether calculated party stats are present (PKHeX's HP-max test)."""

        return self.is_party and self.stat_hp_max != 0

    def _require_party(self, field: str) -> None:
        if not self.is_party:
            raise PA9SizeError(
                f"{field} is available only in a 0x{PARTY_SIZE:X}-byte party PA9"
            )

    def _get_u8(self, offset: int) -> int:
        return self._data[offset]

    def _set_u8(self, offset: int, value: int, field: str) -> None:
        self._data[offset] = _uint(value, 8, field)

    def _get_u16(self, offset: int) -> int:
        return unpack_from("<H", self._data, offset)[0]

    def _set_u16(self, offset: int, value: int, field: str) -> None:
        pack_into("<H", self._data, offset, _uint(value, 16, field))

    def _get_u32(self, offset: int) -> int:
        return unpack_from("<I", self._data, offset)[0]

    def _set_u32(self, offset: int, value: int, field: str) -> None:
        pack_into("<I", self._data, offset, _uint(value, 32, field))

    def _get_string(self, offset: int, size: int) -> str:
        field = bytes(self._data[offset : offset + size])
        end = len(field)
        for index in range(0, len(field), 2):
            if field[index : index + 2] == b"\0\0":
                end = index
                break
        return field[:end].decode("utf-16-le", errors="surrogatepass")

    def _set_string(
        self,
        offset: int,
        size: int,
        maximum_code_units: int,
        value: str,
        field: str,
    ) -> None:
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string")
        encoded = value.encode("utf-16-le", errors="surrogatepass")
        encoded = encoded[: maximum_code_units * 2]
        # Never leave a lone high surrogate when truncation falls between the
        # two code units of a supplementary Unicode character.
        if len(encoded) >= 2:
            final_unit = int.from_bytes(encoded[-2:], "little")
            if 0xD800 <= final_unit <= 0xDBFF:
                encoded = encoded[:-2]
        self._data[offset : offset + len(encoded)] = encoded
        terminator = offset + len(encoded)
        if terminator + 2 <= offset + size:
            self._data[terminator : terminator + 2] = b"\0\0"

    @property
    def ec(self) -> int:
        """Encryption constant (EC), an unsigned 32-bit value."""

        return self._get_u32(0)

    @ec.setter
    def ec(self, value: int) -> None:
        self._set_u32(0, value, "ec")

    @property
    def encryption_constant(self) -> int:
        """Alias for :attr:`ec`."""

        return self.ec

    @encryption_constant.setter
    def encryption_constant(self, value: int) -> None:
        self.ec = value

    @property
    def sanity(self) -> int:
        """PA9 sanity word; valid records use zero."""

        return self._get_u16(4)

    @sanity.setter
    def sanity(self, value: int) -> None:
        self._set_u16(4, value, "sanity")

    @property
    def checksum(self) -> int:
        """Stored additive checksum."""

        return self._get_u16(6)

    @checksum.setter
    def checksum(self, value: int) -> None:
        self._set_u16(6, value, "checksum")

    @property
    def species_internal(self) -> int:
        """Z-A internal species identifier at offset 0x08."""

        return self._get_u16(0x08)

    @species_internal.setter
    def species_internal(self, value: int) -> None:
        self._set_u16(0x08, value, "species_internal")

    @property
    def species(self) -> int:
        """Alias for :attr:`species_internal` (no national-ID conversion)."""

        return self.species_internal

    @species.setter
    def species(self, value: int) -> None:
        self.species_internal = value

    @property
    def item(self) -> int:
        """Held-item identifier at offset 0x0A."""

        return self._get_u16(0x0A)

    @item.setter
    def item(self, value: int) -> None:
        self._set_u16(0x0A, value, "item")

    @property
    def held_item(self) -> int:
        """Alias for :attr:`item`."""

        return self.item

    @held_item.setter
    def held_item(self, value: int) -> None:
        self.item = value

    experience = _integer_property(
        0x10, 32, "experience", "Stored experience points (unsigned 32-bit)."
    )

    @property
    def exp(self) -> int:
        """Alias for :attr:`experience`."""

        return self.experience

    @exp.setter
    def exp(self, value: int) -> None:
        self.experience = value

    ability = _integer_property(
        0x14, 16, "ability", "Ability identifier stored in block A."
    )
    ability_number = _masked_byte_property(
        0x16, 0x07, 0, "ability_number", "Three-bit ability slot value."
    )
    nature = _integer_property(0x20, 8, "nature", "Original nature identifier.")
    stat_nature = _integer_property(
        0x21, 8, "stat_nature", "Stat nature (minted nature) identifier."
    )

    @property
    def stat_alignment(self) -> int:
        """Alias for :attr:`stat_nature`, matching PKHeX's PA9 name."""

        return self.stat_nature

    @stat_alignment.setter
    def stat_alignment(self, value: int) -> None:
        self.stat_nature = value

    fateful_encounter = _boolean_bit_property(
        0x22, 0x01, "fateful_encounter", "Fateful-encounter flag."
    )
    gender = _masked_byte_property(
        0x22, 0x06, 1, "gender", "Gender value (0 male, 1 female, 2 genderless)."
    )
    @property
    def is_alpha(self) -> bool:
        """Whether the PA9 alpha marker byte is non-zero."""

        return self._get_u8(0x23) != 0

    @is_alpha.setter
    def is_alpha(self, value: bool) -> None:
        self._set_u8(0x23, 1 if _boolean(value, "is_alpha") else 0, "is_alpha")

    @property
    def alpha(self) -> bool:
        """Alias for :attr:`is_alpha`."""

        return self.is_alpha

    @alpha.setter
    def alpha(self, value: bool) -> None:
        self.is_alpha = value

    @property
    def form(self) -> int:
        """Form identifier; setting it also clears the alignment byte at 0x25."""

        return self._get_u8(0x24)

    @form.setter
    def form(self, value: int) -> None:
        self._set_u16(0x24, _uint(value, 8, "form"), "form")

    ev_hp = _integer_property(0x26, 8, "ev_hp", "HP effort value.")
    ev_atk = _integer_property(0x27, 8, "ev_atk", "Attack effort value.")
    ev_def = _integer_property(0x28, 8, "ev_def", "Defense effort value.")
    ev_spe = _integer_property(0x29, 8, "ev_spe", "Speed effort value.")
    ev_spa = _integer_property(0x2A, 8, "ev_spa", "Special Attack effort value.")
    ev_spd = _integer_property(0x2B, 8, "ev_spd", "Special Defense effort value.")

    @property
    def tid(self) -> int:
        """16-bit original-trainer public ID."""

        return self._get_u16(0x0C)

    @tid.setter
    def tid(self, value: int) -> None:
        self._set_u16(0x0C, value, "tid")

    @property
    def sid(self) -> int:
        """16-bit original-trainer secret ID."""

        return self._get_u16(0x0E)

    @sid.setter
    def sid(self, value: int) -> None:
        self._set_u16(0x0E, value, "sid")

    @property
    def pid(self) -> int:
        """Pokémon personality identifier (PID)."""

        return self._get_u32(0x1C)

    @pid.setter
    def pid(self, value: int) -> None:
        self._set_u32(0x1C, value, "pid")

    @property
    def nickname(self) -> str:
        """Nickname decoded from the 13-code-unit UTF-16LE buffer."""

        return self._get_string(NICKNAME_OFFSET, NICKNAME_SIZE)

    @nickname.setter
    def nickname(self, value: str) -> None:
        # Existing trailing trash bytes are retained, matching PKHeX's
        # StringConverterOption.None behavior.
        self._set_string(
            NICKNAME_OFFSET,
            NICKNAME_SIZE,
            MAX_NICKNAME_CODE_UNITS,
            value,
            "nickname",
        )

    move1 = _integer_property(0x72, 16, "move1", "First current move identifier.")
    move2 = _integer_property(0x74, 16, "move2", "Second current move identifier.")
    move3 = _integer_property(0x76, 16, "move3", "Third current move identifier.")
    move4 = _integer_property(0x78, 16, "move4", "Fourth current move identifier.")
    move1_pp = _integer_property(0x7A, 8, "move1_pp", "Current PP for move 1.")
    move2_pp = _integer_property(0x7B, 8, "move2_pp", "Current PP for move 2.")
    move3_pp = _integer_property(0x7C, 8, "move3_pp", "Current PP for move 3.")
    move4_pp = _integer_property(0x7D, 8, "move4_pp", "Current PP for move 4.")
    move1_pp_ups = _integer_property(
        0x7E, 8, "move1_pp_ups", "PP Up count for move 1."
    )
    move2_pp_ups = _integer_property(
        0x7F, 8, "move2_pp_ups", "PP Up count for move 2."
    )
    move3_pp_ups = _integer_property(
        0x80, 8, "move3_pp_ups", "PP Up count for move 3."
    )
    move4_pp_ups = _integer_property(
        0x81, 8, "move4_pp_ups", "PP Up count for move 4."
    )
    relearn_move1 = _integer_property(
        0x82, 16, "relearn_move1", "First relearn move identifier."
    )
    relearn_move2 = _integer_property(
        0x84, 16, "relearn_move2", "Second relearn move identifier."
    )
    relearn_move3 = _integer_property(
        0x86, 16, "relearn_move3", "Third relearn move identifier."
    )
    relearn_move4 = _integer_property(
        0x88, 16, "relearn_move4", "Fourth relearn move identifier."
    )
    stat_hp_current = _integer_property(
        0x8A, 16, "stat_hp_current", "Cached current HP value."
    )
    iv32 = _integer_property(0x8C, 32, "iv32", "Packed IV and egg/name flags.")
    iv_hp = _iv_property(0, "iv_hp", "HP individual value.")
    iv_atk = _iv_property(5, "iv_atk", "Attack individual value.")
    iv_def = _iv_property(10, "iv_def", "Defense individual value.")
    iv_spe = _iv_property(15, "iv_spe", "Speed individual value.")
    iv_spa = _iv_property(20, "iv_spa", "Special Attack individual value.")
    iv_spd = _iv_property(25, "iv_spd", "Special Defense individual value.")
    is_egg = _iv_flag_property(0x40000000, "is_egg", "Egg flag.")
    is_nicknamed = _iv_flag_property(
        0x80000000, "is_nicknamed", "Explicit nickname flag."
    )

    version = _integer_property(0xCE, 8, "version", "Origin game version value.")
    language = _integer_property(0xD5, 8, "language", "Language identifier.")

    @property
    def original_trainer_name(self) -> str:
        """Original trainer name from its 13-code-unit UTF-16LE buffer."""

        return self._get_string(TRAINER_NAME_OFFSET, TRAINER_NAME_SIZE)

    @original_trainer_name.setter
    def original_trainer_name(self, value: str) -> None:
        self._set_string(
            TRAINER_NAME_OFFSET,
            TRAINER_NAME_SIZE,
            MAX_TRAINER_NAME_CODE_UNITS,
            value,
            "original_trainer_name",
        )

    @property
    def ot_name(self) -> str:
        """Alias for :attr:`original_trainer_name`."""

        return self.original_trainer_name

    @ot_name.setter
    def ot_name(self, value: str) -> None:
        self.original_trainer_name = value

    ot_friendship = _integer_property(
        0x112, 8, "ot_friendship", "Original trainer friendship value."
    )
    egg_year = _integer_property(0x119, 8, "egg_year", "Egg date year offset.")
    egg_month = _integer_property(0x11A, 8, "egg_month", "Egg date month.")
    egg_day = _integer_property(0x11B, 8, "egg_day", "Egg date day.")
    met_year = _integer_property(0x11C, 8, "met_year", "Met date year offset.")
    met_month = _integer_property(0x11D, 8, "met_month", "Met date month.")
    met_day = _integer_property(0x11E, 8, "met_day", "Met date day.")
    obedience_level = _integer_property(
        0x11F, 8, "obedience_level", "Obedience level value."
    )
    egg_location = _integer_property(
        0x120, 16, "egg_location", "Egg location identifier."
    )
    met_location = _integer_property(
        0x122, 16, "met_location", "Met location identifier."
    )
    ball = _integer_property(0x124, 8, "ball", "Ball identifier.")
    met_level = _masked_byte_property(
        0x125, 0x7F, 0, "met_level", "Met level (lower seven bits)."
    )
    ot_gender = _masked_byte_property(
        0x125, 0x80, 7, "ot_gender", "Original trainer gender bit."
    )

    @property
    def checksum_valid(self) -> bool:
        """Whether the stored checksum matches the canonical block data."""

        return self.checksum == calculate_checksum(self._data)

    @property
    def checksum_ok(self) -> bool:
        """Alias for :attr:`checksum_valid`."""

        return self.checksum_valid

    @property
    def valid(self) -> bool:
        """Whether sanity is zero and the checksum is valid."""

        return self.sanity == 0 and self.checksum_valid

    def update_checksum(self) -> int:
        """Recalculate, store, and return the PA9 checksum."""

        value = calculate_checksum(self._data)
        self.checksum = value
        return value

    def validate(self) -> None:
        """Raise a specific validation error if the canonical record is bad."""

        if self.sanity != 0:
            raise PA9SanityError(
                f"PA9 sanity word must be 0x0000; got 0x{self.sanity:04X}"
            )
        calculated = calculate_checksum(self._data)
        if self.checksum != calculated:
            raise PA9ChecksumError(
                "PA9 checksum mismatch: "
                f"stored 0x{self.checksum:04X}, calculated 0x{calculated:04X}"
            )

    def to_bytes(
        self,
        *,
        encrypted: bool = False,
        party: Optional[bool] = None,
        update_checksum: bool = True,
    ) -> bytes:
        """Export the record as decrypted ``.pa9`` or encrypted bytes.

        Args:
            encrypted: Apply block shuffling and LCRNG encryption.
            party: Preserve the input size when ``None``; choose 0x158 party
                output when ``True`` or 0x148 stored output when ``False``.
            update_checksum: Refresh the object's checksum before exporting.
        """

        if not isinstance(encrypted, bool):
            raise TypeError("encrypted must be a bool")
        if party is not None and not isinstance(party, bool):
            raise TypeError("party must be True, False, or None")
        if not isinstance(update_checksum, bool):
            raise TypeError("update_checksum must be a bool")
        if update_checksum:
            self.update_checksum()

        want_party = self.is_party if party is None else party
        if want_party:
            raw = bytes(self._data).ljust(PARTY_SIZE, b"\0")
        else:
            raw = bytes(self._data[:STORED_SIZE])
        return encrypt_pa9(raw, update_checksum=False) if encrypted else raw

    def save(
        self,
        path: Union[str, Path],
        *,
        encrypted: Optional[bool] = None,
        party: Optional[bool] = None,
        update_checksum: bool = True,
    ) -> Path:
        """Write the record and return the resulting :class:`~pathlib.Path`.

        If *encrypted* is ``None``, ``.ea9`` (PKHeX's encrypted extension) and
        ``.epa9`` paths select encrypted output; other suffixes select normal
        decrypted ``.pa9`` output.
        """

        file_path = Path(path)
        if encrypted is None:
            encrypted = file_path.suffix.lower() in {".ea9", ".epa9"}
        file_path.write_bytes(
            self.to_bytes(
                encrypted=encrypted,
                party=party,
                update_checksum=update_checksum,
            )
        )
        return file_path

    def __repr__(self) -> str:
        return (
            f"PA9(species_internal={self.species_internal}, "
            f"nickname={self.nickname!r}, size=0x{self.size:X}, "
            f"checksum_valid={self.checksum_valid})"
        )


@dataclass(frozen=True)
class PA9Candidate:
    """A checksum-validated PA9 record found inside a larger payload.

    Attributes:
        offset: Byte offset of the candidate in the source payload.
        size: Record size requested from the scanner.
        encrypted: Whether the checksum-valid candidate decoded as encrypted.
        pa9: Decoded canonical :class:`PA9` object.
    """

    offset: int
    size: int
    encrypted: bool
    pa9: PA9

    @property
    def record(self) -> PA9:
        """Alias for :attr:`pa9`."""

        return self.pa9


def scan_pa9_candidates(
    payload: BytesLike,
    *,
    record_size: int = STORED_SIZE,
    start: int = 0,
    end: Optional[int] = None,
    alignment: int = 1,
    require_species: bool = True,
    max_candidates: Optional[int] = None,
) -> list[PA9Candidate]:
    """Scan arbitrary bytes for checksum-valid PA9 records.

    Validation uses the unencrypted header sanity word, PKHeX's encryption
    heuristic, both possible decode states, and the PA9 checksum.  Trying the
    alternate state avoids missing the rare encrypted record whose two
    heuristic words both happen to be zero.  By default empty slots (internal
    species zero) are ignored.  Since the checksum covers only the first 0x148
    bytes, a surrounding payload cannot intrinsically distinguish a stored
    record from a party record; pass ``record_size=PARTY_SIZE`` when the
    protocol framing is known to contain party data.

    Args:
        payload: Larger byte buffer to inspect.
        record_size: ``STORED_SIZE`` (default) or ``PARTY_SIZE``.
        start: First candidate offset, inclusive.
        end: End of the scan region, exclusive; defaults to payload length.
        alignment: Test every Nth byte starting at *start*.
        require_species: Ignore checksum-valid empty slots when true.
        max_candidates: Optional positive result limit.
    """

    raw = _as_bytes(payload, "payload")
    if record_size not in (STORED_SIZE, PARTY_SIZE):
        raise PA9SizeError(
            f"record_size must be {STORED_SIZE} or {PARTY_SIZE}; got {record_size!r}"
        )
    if not isinstance(start, int) or isinstance(start, bool):
        raise TypeError("start must be an integer")
    if end is None:
        end = len(raw)
    if not isinstance(end, int) or isinstance(end, bool):
        raise TypeError("end must be an integer or None")
    if not 0 <= start <= end <= len(raw):
        raise ValueError(
            f"scan bounds must satisfy 0 <= start <= end <= {len(raw)}; "
            f"got start={start}, end={end}"
        )
    if not isinstance(alignment, int) or isinstance(alignment, bool) or alignment <= 0:
        raise ValueError(f"alignment must be a positive integer; got {alignment!r}")
    if not isinstance(require_species, bool):
        raise TypeError("require_species must be a bool")
    if max_candidates is not None and (
        not isinstance(max_candidates, int)
        or isinstance(max_candidates, bool)
        or max_candidates <= 0
    ):
        raise ValueError("max_candidates must be a positive integer or None")

    candidates: list[PA9Candidate] = []
    final_offset = end - record_size
    if final_offset < start:
        return candidates
    empty_record = bytes(record_size)

    for offset in range(start, final_offset + 1, alignment):
        # Sanity is in the unencrypted eight-byte header, so it is a cheap and
        # exact prefilter for both encrypted and canonical records.
        if raw[offset + 4 : offset + 6] != b"\0\0":
            continue
        chunk = raw[offset : offset + record_size]
        encrypted = (
            chunk[0x70:0x72] != b"\0\0" or chunk[0x110:0x112] != b"\0\0"
        )
        # Avoid treating every offset in zero-filled payloads as an empty PA9.
        if require_species and chunk == empty_record:
            continue
        record: Optional[PA9] = None
        decoded_encrypted = encrypted
        for candidate_encrypted in (encrypted, not encrypted):
            try:
                decoded = PA9(
                    chunk, encrypted=candidate_encrypted, validate=True
                )
            except PA9ValidationError:
                continue
            if require_species and decoded.species_internal == 0:
                continue
            record = decoded
            decoded_encrypted = candidate_encrypted
            break
        if record is None:
            continue
        candidates.append(
            PA9Candidate(offset, record_size, decoded_encrypted, record)
        )
        if max_candidates is not None and len(candidates) >= max_candidates:
            break
    return candidates


def scan_pa9(payload: BytesLike, **kwargs: object) -> list[PA9Candidate]:
    """Short alias for :func:`scan_pa9_candidates`."""

    return scan_pa9_candidates(payload, **kwargs)


__all__ = [
    "BLOCK_COUNT",
    "BLOCK_REGION_SIZE",
    "BLOCK_SIZE",
    "HEADER_SIZE",
    "PARTY_SIZE",
    "STORED_SIZE",
    "PA9",
    "PA9Candidate",
    "PA9ChecksumError",
    "PA9Error",
    "PA9SanityError",
    "PA9SizeError",
    "PA9ValidationError",
    "calculate_checksum",
    "crypt_array",
    "crypt_lcrng",
    "decrypt_if_encrypted",
    "decrypt_pa9",
    "encrypt_pa9",
    "is_encrypted_pa9",
    "scan_pa9",
    "scan_pa9_candidates",
    "shuffle_blocks",
    "unshuffle_blocks",
]
