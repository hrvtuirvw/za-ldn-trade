"""Strict Pokemon Legends: Z-A local Link Code advertisement parsing.

The title appends a fixed 20-byte Link Code record to Pia's 0x5C-byte
system-property header.  This module deliberately parses that record by its
measured framing; it never searches arbitrary advertisement bytes for a digit
substring.
"""

from __future__ import annotations

from .session import AdvertisementError, application_communication_version


ZA_LINK_CODE_DIGITS = 8
ZA_LINK_CODE_OFFSET = 0x5C
ZA_LINK_CODE_BUFFER_SIZE = 0x10
ZA_LINK_CODE_LENGTH_OFFSET = 0x6C
ZA_LINK_CODE_APPLICATION_DATA_SIZE = 0x70
ZA_APPLICATION_COMMUNICATION_VERSION = 6


class LinkCodeAdvertisementError(AdvertisementError):
    """The Z-A title-specific portion of an LDN advertisement is malformed."""


def validate_link_code(value: object) -> str:
    """Return an exact eight-ASCII-digit Link Code or raise ``ValueError``."""

    if not isinstance(value, str):
        raise TypeError("Z-A Link Code must be a string")
    if len(value) != ZA_LINK_CODE_DIGITS or any(
        character < "0" or character > "9" for character in value
    ):
        raise ValueError("Z-A Link Code must contain exactly 8 ASCII digits")
    return value


def parse_advertised_link_code(
    application_data: bytes | bytearray | memoryview,
) -> str:
    """Parse the fixed Link Code field from one current Z-A advertisement.

    The measured record is ``char code[16]`` followed by a little-endian
    32-bit length.  An eight-digit code occupies the first half of the buffer;
    the unused half is zero.  Requiring the complete Pia header, exact total
    length, padding, and length word prevents unrelated application bytes from
    being mistaken for a room code.
    """

    if not isinstance(application_data, (bytes, bytearray, memoryview)):
        raise TypeError("application_data must be bytes-like")
    data = bytes(application_data)
    if len(data) != ZA_LINK_CODE_APPLICATION_DATA_SIZE:
        raise LinkCodeAdvertisementError(
            "unexpected Z-A Link Code advertisement size "
            f"0x{len(data):X} (expected 0x{ZA_LINK_CODE_APPLICATION_DATA_SIZE:X})"
        )

    try:
        version = application_communication_version(data)
    except AdvertisementError as exc:
        raise LinkCodeAdvertisementError(
            "malformed Pia system-property header in Z-A advertisement"
        ) from exc
    if version != ZA_APPLICATION_COMMUNICATION_VERSION:
        raise LinkCodeAdvertisementError(
            "unsupported Z-A application communication version "
            f"{version} (expected {ZA_APPLICATION_COMMUNICATION_VERSION})"
        )

    declared_length = int.from_bytes(
        data[ZA_LINK_CODE_LENGTH_OFFSET:ZA_LINK_CODE_APPLICATION_DATA_SIZE],
        "little",
    )
    if declared_length != ZA_LINK_CODE_DIGITS:
        raise LinkCodeAdvertisementError(
            "Z-A Link Code record does not declare an 8-byte value"
        )

    buffer_end = ZA_LINK_CODE_OFFSET + ZA_LINK_CODE_BUFFER_SIZE
    code_bytes = data[
        ZA_LINK_CODE_OFFSET : ZA_LINK_CODE_OFFSET + ZA_LINK_CODE_DIGITS
    ]
    padding = data[
        ZA_LINK_CODE_OFFSET + ZA_LINK_CODE_DIGITS : buffer_end
    ]
    if padding != bytes(ZA_LINK_CODE_BUFFER_SIZE - ZA_LINK_CODE_DIGITS):
        raise LinkCodeAdvertisementError(
            "Z-A Link Code record has nonzero fixed-buffer padding"
        )
    if len(code_bytes) != ZA_LINK_CODE_DIGITS or any(
        value < 0x30 or value > 0x39 for value in code_bytes
    ):
        raise LinkCodeAdvertisementError(
            "advertised Z-A Link Code is not exactly 8 ASCII digits"
        )
    return code_bytes.decode("ascii")


def advertised_link_code_matches(
    application_data: bytes | bytearray | memoryview,
    expected: object,
) -> bool:
    """Return true only for a structurally valid advertisement of ``expected``."""

    code = validate_link_code(expected)
    try:
        advertised = parse_advertised_link_code(application_data)
    except (TypeError, ValueError):
        return False
    return advertised == code


__all__ = [
    "LinkCodeAdvertisementError",
    "ZA_APPLICATION_COMMUNICATION_VERSION",
    "ZA_LINK_CODE_APPLICATION_DATA_SIZE",
    "ZA_LINK_CODE_BUFFER_SIZE",
    "ZA_LINK_CODE_DIGITS",
    "ZA_LINK_CODE_LENGTH_OFFSET",
    "ZA_LINK_CODE_OFFSET",
    "advertised_link_code_matches",
    "parse_advertised_link_code",
    "validate_link_code",
]
