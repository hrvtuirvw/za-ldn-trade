"""Generic Pia 6.32--6.40 packet primitives for Nintendo LDN traffic.

This module intentionally stops at the Pia transport/message boundary.  It can
decrypt captures and construct synthetic packets, but it does not implement any
Pokemon-specific application protocol or automate interaction with a game.
"""

from __future__ import annotations

import ipaddress
import zlib
from dataclasses import dataclass, replace
from typing import Any, Sequence

from Crypto.Cipher import AES

from .profile import PiaProfile, ZA_PROFILE

try:
    import zstandard as _zstd
except ImportError:  # pragma: no cover - exercised by callers without the extra
    _zstd = None


PIA_MAGIC = bytes.fromhex("32ab9864")
ZSTD_MAGIC = bytes.fromhex("28b52ffd")
PIA_ZSTD_WINDOW_DESCRIPTOR = 0x18
HEADER_SIZE = 29
NONCE_OFFSET = 13
TAG_OFFSET = 21
PAYLOAD_OFFSET = HEADER_SIZE
SUPPORTED_HEADER_VERSIONS = frozenset((15, 16))

PACKET_FLAG_ZSTD = 0x01
PACKET_FLAG_SKIP_DESTINATION_CHECK = 0x02
PACKET_FLAG_SKIP_NETWORK_CHECK = 0x04

MESSAGE_FIELD_FLAGS = 0x01
MESSAGE_FIELD_SIZE = 0x02
MESSAGE_FIELD_PROTOCOL = 0x04
MESSAGE_FIELD_PORT = 0x08
MESSAGE_FIELD_PROTOCOL_SPECIFIC = 0x10
MESSAGE_FIELD_MASK = 0x1F
MAX_MESSAGE_PAYLOAD_SIZE = 0x58F
# A decompressed compact-message stream is still a single Pia packet payload.
# Bound it to the title profile's packet buffer instead of trusting a ZSTD
# frame's advertised content size.  In particular, python-zstandard's
# ``max_output_size`` is ignored when a frame declares its content size, so
# callers must reject an oversized declaration before decompression.
MAX_DECOMPRESSED_MESSAGE_SIZE = ZA_PROFILE.max_packet_size
# Native Pia 6.40 uses the literal 0x7F when a compact header must not
# reference the preceding cached header.  The serializer/parser still inspect
# only the five low presence bits; bits 5--7 carry no additional fields.
MESSAGE_HEADER_FORCE_FULL = 0x60

HAVE_ZSTD = _zstd is not None


class PiaError(ValueError):
    """Base class for malformed or unauthentic Pia traffic."""


class InvalidHeaderError(PiaError):
    """The fixed 29-byte Pia header is missing or malformed."""


class AuthenticationError(PiaError):
    """AES-GCM authentication failed."""


class PaddingError(PiaError):
    """The authenticated payload has inconsistent 0xFF padding."""


class CompressionError(PiaError):
    """A packet marked as ZSTD could not be compressed or decompressed."""


class MessageParseError(PiaError):
    """A compact Pia message stream is incomplete or ambiguous."""


class PacketTooLargeError(PiaError):
    """An encoded packet exceeds the profile's receive limit."""


def _bytes(value: bytes | bytearray | memoryview, name: str) -> bytes:
    try:
        return bytes(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be bytes-like") from exc


def ipv4_bytes(address: str | int | bytes | bytearray | ipaddress.IPv4Address) -> bytes:
    """Return a four-byte, network-order IPv4 address."""

    if isinstance(address, (bytes, bytearray)):
        packed = bytes(address)
        if len(packed) != 4:
            raise ValueError("an IPv4 byte string must contain exactly four bytes")
        return packed
    try:
        return ipaddress.IPv4Address(address).packed
    except ipaddress.AddressValueError as exc:
        raise ValueError(f"invalid IPv4 address: {address!r}") from exc


@dataclass(frozen=True)
class PiaHeader:
    """Pia's complete 29-byte plaintext header (versions 15 and 16)."""

    dst: int = 0
    src: int = 0
    packet_id: int = 0
    nonce: bytes = b"\x00" * 8
    footer_size: int = 0
    flags: int = 0
    padding_size: int = 0
    encrypted: bool = True
    header_version: int = 16
    tag: bytes = b"\x00" * 8

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("dst", self.dst, 0xFFFF),
            ("src", self.src, 0xFFFF),
            ("packet_id", self.packet_id, 0xFFFF),
            ("footer_size", self.footer_size, 0xFF),
            ("flags", self.flags, 0x0F),
            ("padding_size", self.padding_size, 0x0F),
        ):
            if not isinstance(value, int) or not 0 <= value <= maximum:
                raise ValueError(f"{name} must be an integer from 0 to {maximum}")
        if self.header_version not in SUPPORTED_HEADER_VERSIONS:
            raise ValueError("Pia 6.32-6.40 uses header version 15 or 16")

        nonce = _bytes(self.nonce, "nonce")
        tag = _bytes(self.tag, "tag")
        if len(nonce) != 8:
            raise ValueError("the header nonce must be exactly 8 bytes")
        if len(tag) != 8:
            raise ValueError("the AES-GCM tag must be exactly 8 bytes")
        object.__setattr__(self, "nonce", nonce)
        object.__setattr__(self, "tag", tag)

    @property
    def enc(self) -> int:
        """The raw encryption/version byte at offset 4."""

        return self.header_version | (0x80 if self.encrypted else 0)

    @property
    def flags_byte(self) -> int:
        """The raw padding/packet-flags byte at offset 5."""

        return (self.padding_size << 4) | self.flags

    # Header field aliases shared by the protocol adapters.
    @property
    def pktid(self) -> int:
        return self.packet_id

    @property
    def nonce8(self) -> bytes:
        return self.nonce

    @property
    def footer(self) -> int:
        return self.footer_size

    def pack(self) -> bytes:
        """Serialize the complete fixed header, including the eight-byte tag."""

        packed = (
            PIA_MAGIC
            + bytes((self.enc, self.flags_byte))
            + self.dst.to_bytes(2, "big")
            + self.src.to_bytes(2, "big")
            + self.packet_id.to_bytes(2, "big")
            + bytes((self.footer_size,))
            + self.nonce
            + self.tag
        )
        if len(packed) != HEADER_SIZE:  # defensive invariant
            raise AssertionError("internal Pia header size mismatch")
        return packed

    @classmethod
    def unpack(cls, datagram: bytes | bytearray | memoryview) -> "PiaHeader":
        """Parse a header from a header-sized buffer or complete datagram."""

        data = _bytes(datagram, "datagram")
        if len(data) < HEADER_SIZE:
            raise InvalidHeaderError(
                f"Pia datagram is {len(data)} bytes; at least {HEADER_SIZE} are required"
            )
        if data[:4] != PIA_MAGIC:
            raise InvalidHeaderError("Pia magic number is missing")

        enc = data[4]
        version = enc & 0x7F
        if version not in SUPPORTED_HEADER_VERSIONS:
            raise InvalidHeaderError(
                f"unsupported Pia header version {version}; expected 15 or 16"
            )
        flags_byte = data[5]
        return cls(
            dst=int.from_bytes(data[6:8], "big"),
            src=int.from_bytes(data[8:10], "big"),
            packet_id=int.from_bytes(data[10:12], "big"),
            footer_size=data[12],
            nonce=data[NONCE_OFFSET:TAG_OFFSET],
            tag=data[TAG_OFFSET:PAYLOAD_OFFSET],
            flags=flags_byte & 0x0F,
            padding_size=flags_byte >> 4,
            encrypted=bool(enc & 0x80),
            header_version=version,
        )

    def to_summary(self) -> dict[str, Any]:
        """Return only JSON-native values; binary fields are hexadecimal."""

        return {
            "header_version": self.header_version,
            "encrypted": self.encrypted,
            "flags": self.flags,
            "padding_size": self.padding_size,
            "destination": self.dst,
            "source": self.src,
            "packet_id": self.packet_id,
            "footer_size": self.footer_size,
            "nonce": self.nonce.hex(),
            "tag": self.tag.hex(),
        }


def is_pia(datagram: bytes | bytearray | memoryview) -> bool:
    data = bytes(datagram)
    return len(data) >= HEADER_SIZE and data[:4] == PIA_MAGIC


class PiaCrypto:
    """Derive the LDN session key and apply Pia's truncated-tag AES-GCM."""

    def __init__(
        self,
        ssid: bytes | bytearray | memoryview,
        game_key: bytes | bytearray | memoryview | str | None = None,
        *,
        profile: PiaProfile = ZA_PROFILE,
    ) -> None:
        self.ssid = _bytes(ssid, "ssid")
        if len(self.ssid) != 16:
            raise ValueError("the Pia 6.16-6.42 LDN SSID must be exactly 16 bytes")

        if game_key is None:
            game_key = profile.game_key
        if isinstance(game_key, str):
            try:
                game_key = game_key.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError("game_key text must be ASCII") from exc
        self.game_key = _bytes(game_key, "game_key")
        if len(self.game_key) != 16:
            raise ValueError("the Pia AES game key must be exactly 16 bytes")

        self.profile = profile
        self.session_key = AES.new(self.game_key, AES.MODE_ECB).encrypt(self.ssid)
        # LDN's network id for these titles is CRC32 over bytes 1..15 of
        # the 16-byte SSID (the common Pia derivation).
        self.network_id = zlib.crc32(self.ssid[1:16]) & 0xFFFFFFFF
        self.net_id = self.network_id

    def nonce(
        self,
        src_ip: str | int | bytes | bytearray | ipaddress.IPv4Address,
        header_nonce: bytes | bytearray | memoryview,
    ) -> bytes:
        """Build ``(network_id XOR source_ipv4) || header_nonce``."""

        nonce8 = _bytes(header_nonce, "header_nonce")
        if len(nonce8) != 8:
            raise ValueError("the header nonce must be exactly 8 bytes")
        ip_value = int.from_bytes(ipv4_bytes(src_ip), "big")
        prefix = (self.network_id ^ ip_value).to_bytes(4, "big")
        return prefix + nonce8

    def encrypt(
        self,
        plaintext: bytes | bytearray | memoryview,
        src_ip: str | int | bytes | bytearray | ipaddress.IPv4Address,
        header: PiaHeader,
    ) -> bytes:
        """Encrypt and return a complete datagram, adding protocol 0xFF padding."""

        if not isinstance(header, PiaHeader):
            raise TypeError("header must be a PiaHeader")
        if not header.encrypted:
            raise InvalidHeaderError("AES-GCM cannot encode an unencrypted header")

        clear = _bytes(plaintext, "plaintext")
        padding_size = (-len(clear)) % 16
        padded = clear + (b"\xFF" * padding_size)
        unsigned_header = replace(
            header,
            padding_size=padding_size,
            tag=b"\x00" * 8,
        )
        cipher = AES.new(
            self.session_key,
            AES.MODE_GCM,
            nonce=self.nonce(src_ip, unsigned_header.nonce),
            mac_len=8,
        )
        ciphertext, tag = cipher.encrypt_and_digest(padded)
        return replace(unsigned_header, tag=tag).pack() + ciphertext

    def decrypt_with_header(
        self,
        datagram: bytes | bytearray | memoryview,
        src_ip: str | int | bytes | bytearray | ipaddress.IPv4Address,
    ) -> tuple[PiaHeader, bytes]:
        """Authenticate, decrypt and remove the padding described by the header."""

        data = _bytes(datagram, "datagram")
        header = PiaHeader.unpack(data)
        if not header.encrypted:
            raise InvalidHeaderError("this Pia packet is not marked as encrypted")

        cipher = AES.new(
            self.session_key,
            AES.MODE_GCM,
            nonce=self.nonce(src_ip, header.nonce),
            mac_len=8,
        )
        try:
            padded = cipher.decrypt_and_verify(data[PAYLOAD_OFFSET:], header.tag)
        except ValueError as exc:
            raise AuthenticationError(
                "AES-GCM authentication failed (wrong SSID/key/source IP or modified packet)"
            ) from exc

        if header.padding_size:
            if len(padded) < header.padding_size:
                raise PaddingError("padding size is larger than the authenticated payload")
            padding = padded[-header.padding_size :]
            if padding != b"\xFF" * header.padding_size:
                raise PaddingError("authenticated Pia padding is not all 0xFF")
            padded = padded[: -header.padding_size]
        return header, padded

    def decrypt(
        self,
        datagram: bytes | bytearray | memoryview,
        src_ip: str | int | bytes | bytearray | ipaddress.IPv4Address,
    ) -> bytes:
        """Authenticate/decrypt a datagram and return its unpadded wire payload."""

        return self.decrypt_with_header(datagram, src_ip)[1]


def compress_zstd(
    payload: bytes | bytearray | memoryview, *, level: int = 4
) -> bytes:
    """Create the dictionary-free 8-KiB-window ZSTD frame emitted by Pia."""

    if _zstd is None:
        raise CompressionError("zstandard is required to encode compressed Pia packets")
    try:
        frame = _zstd.ZstdCompressor(
            level=level,
            write_content_size=False,
            write_checksum=False,
            write_dict_id=False,
        ).compress(_bytes(payload, "payload"))
        # python-zstandard chooses the smallest legal window (0x00 for these
        # short packets), while native Pia 6.40 consistently emits 0x18.  The
        # compressed blocks are identical; widening the declared window is
        # safe because a Pia datagram is below 1.5 KiB and restores the exact
        # frame form observed from both Switch peers.
        fhd = frame[4]
        if (fhd & 0x03) or (fhd & 0x04):
            return frame
        content_size_flag = fhd >> 6
        if fhd & 0x20:
            blocks = frame[5 + (1, 2, 4, 8)[content_size_flag] :]
        else:
            if frame[5] > PIA_ZSTD_WINDOW_DESCRIPTOR:
                return frame
            blocks = frame[6 + (0, 2, 4, 8)[content_size_flag] :]
        return ZSTD_MAGIC + bytes((0, PIA_ZSTD_WINDOW_DESCRIPTOR)) + blocks
    except Exception as exc:
        raise CompressionError(f"ZSTD compression failed: {exc}") from exc


def decompress_zstd(
    payload: bytes | bytearray | memoryview,
    *,
    max_output_size: int = MAX_DECOMPRESSED_MESSAGE_SIZE,
) -> bytes:
    """Decode one Pia ZSTD frame without exceeding *max_output_size* bytes."""

    data = _bytes(payload, "payload")
    if (
        not isinstance(max_output_size, int)
        or isinstance(max_output_size, bool)
        or max_output_size <= 0
    ):
        raise ValueError("max_output_size must be a positive integer")
    if not data.startswith(ZSTD_MAGIC):
        raise CompressionError("packet flag says ZSTD but the frame magic is missing")
    if _zstd is None:
        raise CompressionError("zstandard is required to decode compressed Pia packets")
    try:
        declared_size = _zstd.frame_content_size(data)
        if declared_size == _zstd.CONTENTSIZE_ERROR:
            raise CompressionError("invalid ZSTD frame header")
        if (
            declared_size != _zstd.CONTENTSIZE_UNKNOWN
            and declared_size > max_output_size
        ):
            raise CompressionError(
                f"ZSTD frame declares {declared_size} decompressed bytes; "
                f"limit is {max_output_size}"
            )
        # Streaming reads bound even an inconsistent declared-empty frame.
        # Validate the whole input before the non-streaming boundary check:
        # no individual frame can then expand beyond this proven total bound.
        with _zstd.ZstdDecompressor().stream_reader(
            data, read_across_frames=True
        ) as reader:
            decoded = reader.read(max_output_size + 1)
            if len(decoded) > max_output_size or reader.read(1):
                raise CompressionError(
                    f"ZSTD output exceeds {max_output_size} bytes"
                )
        # The pinned backend ignores allow_extra_data=False for unknown-size
        # frames; streaming alone accepts truncated EOFs. Require exactly one
        # complete frame as well. Pia's footer has already been split off.
        frame = _zstd.ZstdDecompressor().decompressobj()
        checked = frame.decompress(data)
        if (not frame.eof or frame.unused_data or frame.unconsumed_tail
                or checked != decoded):
            raise CompressionError("Pia requires exactly one complete ZSTD frame")
        return decoded
    except CompressionError:
        raise
    except Exception as exc:
        raise CompressionError(f"ZSTD decompression failed: {exc}") from exc


# Descriptive aliases for callers that do not care about the wire codec name.
compress_payload = compress_zstd
decompress_payload = decompress_zstd


# Pia 6.32-6.40 protocol ids from the public NintendoClients protocol table.
PROTOCOL_NAMES: dict[int, str] = {
    1: "Net",
    2: "Turn",
    3: "RTT",
    4: "Sync",
    5: "Unreliable",
    6: "Clone (atomic)",
    7: "Clone (event)",
    8: "Clone (broadcast event)",
    9: "Clone (clock)",
    10: "Reliable",
    11: "Broadcast Reliable",
    12: "Stream Broadcast Reliable",
    13: "Session",
    14: "NAT Traversal Result",
    15: "Monitoring Data",
    16: "WAN NAT Traversal",
    17: "Reckoning 1D",
    18: "Reckoning 3D",
}


def protocol_name(protocol: int) -> str:
    return PROTOCOL_NAMES.get(protocol, f"Unknown ({protocol})")


@dataclass(frozen=True)
class PiaMessage:
    """One decoded Pia 6.32--6.40 compact message."""

    flags: int
    msgflags: int
    size: int
    proto: int
    port: int
    protocol_specific: int
    payload: bytes
    offset: int = 0
    hdr_bytes: bytes = b""

    @property
    def protocol(self) -> int:
        return self.proto

    @property
    def protocol_name(self) -> str:
        return protocol_name(self.proto)

    @property
    def message_flags(self) -> int:
        return self.msgflags

    @property
    def payload_size(self) -> int:
        return self.size

    @property
    def presence_flags(self) -> int:
        """Return the five field-presence bits, excluding control bits."""

        return self.flags & MESSAGE_FIELD_MASK

    @property
    def force_full_header(self) -> bool:
        """Whether the native no-reference marker (bits 5 and 6) is set."""

        return (self.flags & MESSAGE_HEADER_FORCE_FULL) == MESSAGE_HEADER_FORCE_FULL

    def serialize(self) -> bytes:
        return self.hdr_bytes + self.payload

    def to_summary(self, payload_preview: int = 32) -> dict[str, Any]:
        preview = self.payload[:payload_preview]
        return {
            "offset": self.offset,
            "field_flags": self.flags,
            "message_flags": self.msgflags,
            "payload_size": self.size,
            "protocol": self.proto,
            "protocol_name": self.protocol_name,
            "port": self.port,
            "protocol_specific": self.protocol_specific,
            "payload_hex": preview.hex(),
            "payload_truncated": len(preview) != len(self.payload),
        }


def parse_messages(data: bytes | bytearray | memoryview) -> list[PiaMessage]:
    """Parse a complete Pia 6.32--6.40 compact-message stream.

    A cleared presence bit inherits that field from the preceding message.
    At the start of each datagram Pia 6.40 uses its native default header:
    message flags/size/port are zero, protocol is ``0xFF``, and the
    protocol-specific byte is ``0xFD``.  Reconstructed fields are then carried
    only between messages in this one stream; there is no cross-datagram cache.
    """

    raw = _bytes(data, "data")
    messages: list[PiaMessage] = []
    cursor = 0
    msgflags = 0
    size = 0
    proto = 0xFF
    port = 0
    protocol_specific = 0xFD

    def take(count: int, field: str, message_offset: int) -> bytes:
        nonlocal cursor
        end = cursor + count
        if end > len(raw):
            raise MessageParseError(
                f"message at offset {message_offset} is truncated in {field}"
            )
        value = raw[cursor:end]
        cursor = end
        return value

    while cursor < len(raw):
        message_offset = cursor
        flags = take(1, "presence flags", message_offset)[0]
        if flags & MESSAGE_FIELD_FLAGS:
            msgflags = take(1, "message flags", message_offset)[0]
        if flags & MESSAGE_FIELD_SIZE:
            size = int.from_bytes(take(2, "payload size", message_offset), "big")
        if size > MAX_MESSAGE_PAYLOAD_SIZE:
            raise MessageParseError(
                f"message at offset {message_offset} declares 0x{size:X} payload "
                f"bytes; Z-A accepts at most 0x{MAX_MESSAGE_PAYLOAD_SIZE:X}"
            )
        if flags & MESSAGE_FIELD_PROTOCOL:
            proto = take(1, "protocol", message_offset)[0]
        if flags & MESSAGE_FIELD_PORT:
            port = take(1, "protocol port", message_offset)[0]
        if flags & MESSAGE_FIELD_PROTOCOL_SPECIFIC:
            protocol_specific = take(1, "protocol-specific byte", message_offset)[0]

        header_end = cursor
        payload = take(size, f"{size}-byte payload", message_offset)
        messages.append(
            PiaMessage(
                flags=flags,
                msgflags=msgflags,
                size=size,
                proto=proto,
                port=port,
                protocol_specific=protocol_specific,
                payload=payload,
                offset=message_offset,
                hdr_bytes=raw[message_offset:header_end],
            )
        )
    return messages


def build_message(
    protocol: int,
    payload: bytes | bytearray | memoryview,
    msgflags: int | None = None,
    *,
    port: int | None = None,
    protocol_specific: int | None = None,
    force_full_header: bool = False,
) -> bytes:
    """Build a self-contained compact message.

    Size and protocol are always present.  With ``force_full_header=True`` all
    five fields are emitted and the native Pia 6.40 no-reference marker makes
    the raw mask ``0x7F``.  This is appropriate for the first message in a
    stateless, standalone datagram.
    """

    if not isinstance(protocol, int) or not 0 <= protocol <= 0xFF:
        raise ValueError("protocol must fit in one byte")
    body = _bytes(payload, "payload")
    if len(body) > MAX_MESSAGE_PAYLOAD_SIZE:
        raise ValueError(
            "a Z-A compact message payload cannot exceed "
            f"0x{MAX_MESSAGE_PAYLOAD_SIZE:X} bytes"
        )
    if not isinstance(force_full_header, bool):
        raise TypeError("force_full_header must be a bool")

    if force_full_header:
        msgflags = 0 if msgflags is None else msgflags
        port = 0 if port is None else port
        # The native per-datagram ProtocolIdentifier default is 0xFD.  A
        # forced full header must preserve the same semantic value that an
        # omitted PS field would inherit.
        protocol_specific = 0xFD if protocol_specific is None else protocol_specific

    flags = MESSAGE_FIELD_SIZE | MESSAGE_FIELD_PROTOCOL
    header = bytearray()
    if msgflags is not None:
        if not 0 <= msgflags <= 0xFF:
            raise ValueError("msgflags must fit in one byte")
        flags |= MESSAGE_FIELD_FLAGS
    if port is not None:
        if not 0 <= port <= 0xFF:
            raise ValueError("port must fit in one byte")
        flags |= MESSAGE_FIELD_PORT
    if protocol_specific is not None:
        if not 0 <= protocol_specific <= 0xFF:
            raise ValueError("protocol_specific must fit in one byte")
        flags |= MESSAGE_FIELD_PROTOCOL_SPECIFIC
    if force_full_header:
        flags |= MESSAGE_HEADER_FORCE_FULL

    header.append(flags)
    if msgflags is not None:
        header.append(msgflags)
    header.extend(len(body).to_bytes(2, "big"))
    header.append(protocol)
    if port is not None:
        header.append(port)
    if protocol_specific is not None:
        header.append(protocol_specific)
    return bytes(header) + body


@dataclass(frozen=True)
class DecodedDatagram:
    """The successive authenticated, decompressed, and parsed packet layers."""

    header: PiaHeader
    plaintext: bytes
    application: bytes
    message_bytes: bytes
    footer: bytes
    messages: Sequence[PiaMessage]
    compressed: bool

    def to_summary(self, payload_preview: int = 32) -> dict[str, Any]:
        """Return a bounded, secret-free, JSON-serializable packet summary."""

        app_preview = self.message_bytes[:payload_preview]
        return {
            "header": self.header.to_summary(),
            "compressed": self.compressed,
            "wire_plaintext_size": len(self.plaintext),
            "application_size": len(self.application),
            "message_bytes_size": len(self.message_bytes),
            "message_bytes_hex": app_preview.hex(),
            "message_bytes_truncated": len(app_preview) != len(self.message_bytes),
            "footer_hex": self.footer.hex(),
            "messages": [
                message.to_summary(payload_preview=payload_preview)
                for message in self.messages
            ],
        }

    def summary(self, payload_preview: int = 32) -> dict[str, Any]:
        return self.to_summary(payload_preview=payload_preview)


def _resolve_crypto(
    profile: PiaProfile,
    ssid: bytes | bytearray | memoryview | None,
    crypto: PiaCrypto | None,
) -> PiaCrypto:
    if crypto is not None:
        if ssid is not None:
            raise TypeError("pass either crypto or ssid, not both")
        if not isinstance(crypto, PiaCrypto):
            raise TypeError("crypto must be a PiaCrypto")
        return crypto
    if ssid is None:
        raise TypeError("ssid is required when crypto is not supplied")
    return PiaCrypto(ssid, profile=profile)


def encode_datagram(
    message_bytes: bytes | bytearray | memoryview,
    src_ip: str | int | bytes | bytearray | ipaddress.IPv4Address,
    header: PiaHeader,
    profile: PiaProfile = ZA_PROFILE,
    *,
    ssid: bytes | bytearray | memoryview | None = None,
    crypto: PiaCrypto | None = None,
    footer: bytes | bytearray | memoryview = b"",
    compress: bool = False,
    enforce_max_size: bool = True,
) -> bytes:
    """Encode compact messages and an encrypted footer into a Pia datagram."""

    if not isinstance(header, PiaHeader):
        raise TypeError("header must be a PiaHeader")
    compact = _bytes(message_bytes, "message_bytes")
    footer_bytes = _bytes(footer, "footer")
    if len(footer_bytes) > 0xFF:
        raise ValueError("Pia footer size must fit in one byte")

    flags = header.flags
    if compress:
        # Pia's packet flag applies to the message payload, not the routing
        # footer.  In 6.32+ both are encrypted together, but captures show the
        # footer remains outside the ZSTD frame.
        wire_messages = compress_zstd(compact, level=profile.zstd_level)
        flags |= PACKET_FLAG_ZSTD
    else:
        wire_messages = compact
        flags &= ~PACKET_FLAG_ZSTD
    wire_payload = wire_messages + footer_bytes

    wire_header = replace(
        header,
        flags=flags,
        footer_size=len(footer_bytes),
        header_version=profile.header_version,
    )
    datagram = _resolve_crypto(profile, ssid, crypto).encrypt(
        wire_payload, src_ip, wire_header
    )
    if enforce_max_size and len(datagram) > profile.max_packet_size:
        raise PacketTooLargeError(
            f"encoded datagram is {len(datagram)} bytes; "
            f"{profile.name} allows {profile.max_packet_size}"
        )
    return datagram


def decode_datagram(
    datagram: bytes | bytearray | memoryview,
    src_ip: str | int | bytes | bytearray | ipaddress.IPv4Address,
    profile: PiaProfile = ZA_PROFILE,
    *,
    ssid: bytes | bytearray | memoryview | None = None,
    crypto: PiaCrypto | None = None,
    parse: bool = True,
) -> DecodedDatagram:
    """Authenticate and decode all generic Pia layers of one UDP datagram."""

    header, plaintext = _resolve_crypto(profile, ssid, crypto).decrypt_with_header(
        datagram, src_ip
    )
    if header.header_version != profile.header_version:
        raise InvalidHeaderError(
            f"packet header version {header.header_version} does not match "
            f"{profile.name} ({profile.header_version})"
        )

    compressed = bool(header.flags & PACKET_FLAG_ZSTD)
    if header.footer_size > len(plaintext):
        raise PiaError(
            f"footer size {header.footer_size} exceeds the "
            f"{len(plaintext)}-byte authenticated payload"
        )
    if header.footer_size:
        wire_messages = plaintext[: -header.footer_size]
        footer = plaintext[-header.footer_size :]
    else:
        wire_messages = plaintext
        footer = b""
    message_blob = (
        decompress_zstd(wire_messages, max_output_size=profile.max_packet_size)
        if compressed
        else wire_messages
    )
    application = message_blob + footer

    messages: Sequence[PiaMessage]
    messages = tuple(parse_messages(message_blob)) if parse else ()
    return DecodedDatagram(
        header=header,
        plaintext=plaintext,
        application=application,
        message_bytes=message_blob,
        footer=footer,
        messages=messages,
        compressed=compressed,
    )


def json_safe_summary(
    decoded: DecodedDatagram, payload_preview: int = 32
) -> dict[str, Any]:
    """Top-level convenience wrapper for capture/CLI serializers."""

    if not isinstance(decoded, DecodedDatagram):
        raise TypeError("decoded must be a DecodedDatagram")
    return decoded.to_summary(payload_preview=payload_preview)
