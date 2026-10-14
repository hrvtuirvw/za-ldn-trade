from __future__ import annotations

import json
from dataclasses import replace

import pytest

from zasim.pia import (
    AuthenticationError,
    CompressionError,
    HAVE_ZSTD,
    HEADER_SIZE,
    InvalidHeaderError,
    MAX_DECOMPRESSED_MESSAGE_SIZE,
    MAX_MESSAGE_PAYLOAD_SIZE,
    MESSAGE_FIELD_PROTOCOL,
    MESSAGE_FIELD_SIZE,
    MessageParseError,
    PACKET_FLAG_ZSTD,
    PaddingError,
    PiaCrypto,
    PiaHeader,
    build_message,
    compress_zstd,
    decode_datagram,
    decompress_zstd,
    encode_datagram,
    json_safe_summary,
    parse_messages,
    protocol_name,
)
from zasim.profile import (
    ZA_GAME_KEY,
    ZA_LDN_PASSPHRASE,
    ZA_PROFILE,
    ZA_TITLE_ID,
)


SSID = bytes(range(16))
SOURCE_IP = "169.254.21.2"


def test_za_public_profile_values_are_exact() -> None:
    assert ZA_TITLE_ID == "0100F43008C44000"
    assert ZA_PROFILE.title_id == ZA_TITLE_ID
    assert ZA_PROFILE.title_id_int == 0x0100F43008C44000
    assert ZA_PROFILE.pia_version == "6.40"
    assert ZA_PROFILE.header_version == 16
    assert ZA_PROFILE.max_packet_size == 1452
    assert ZA_GAME_KEY == b"p3bwdaSsywFXUkDu"
    assert len(ZA_GAME_KEY) == 16
    assert ZA_LDN_PASSPHRASE == (
        b"BM7cXkadR9ugiXdHiurkiyhrQwcR3rMgCM5BF47dranKXWAGpGEA9z3ncXRnPjCX"
    )
    assert len(ZA_LDN_PASSPHRASE) == 64


def test_profile_rejects_wrong_secret_lengths() -> None:
    with pytest.raises(ValueError, match="64 bytes"):
        replace(ZA_PROFILE, ldn_passphrase=b"short")
    with pytest.raises(ValueError, match="16 bytes"):
        replace(ZA_PROFILE, game_key=b"short")


@pytest.mark.parametrize("version", [15, 16])
def test_29_byte_header_roundtrip(version: int) -> None:
    header = PiaHeader(
        dst=0x1234,
        src=0xABCD,
        packet_id=0xFEDC,
        nonce=bytes.fromhex("0102030405060708"),
        footer_size=4,
        flags=0x05,
        padding_size=0x0B,
        encrypted=True,
        header_version=version,
        tag=bytes.fromhex("a1a2a3a4a5a6a7a8"),
    )

    packed = header.pack()

    assert len(packed) == HEADER_SIZE == 29
    assert packed[:4].hex() == "32ab9864"
    assert packed[4] == 0x80 | version
    assert packed[5] == 0xB5
    assert packed[6:8].hex() == "1234"
    assert packed[8:10].hex() == "abcd"
    assert packed[10:12].hex() == "fedc"
    assert packed[13:21] == header.nonce
    assert packed[21:29] == header.tag
    assert PiaHeader.unpack(packed + b"ciphertext") == header


def test_header_errors_are_explicit() -> None:
    with pytest.raises(InvalidHeaderError, match="at least 29"):
        PiaHeader.unpack(b"\x00" * 28)
    with pytest.raises(InvalidHeaderError, match="magic"):
        PiaHeader.unpack(b"\x00" * 29)

    unsupported = bytearray(PiaHeader().pack())
    unsupported[4] = 0x80 | 14
    with pytest.raises(InvalidHeaderError, match="version 14"):
        PiaHeader.unpack(unsupported)


def test_session_key_network_id_and_nonce_known_vector() -> None:
    crypto = PiaCrypto(SSID)

    assert crypto.session_key.hex() == "75e4eef6750c82e513b3f62b87fe9373"
    assert crypto.network_id == 0xF5A6AA3A
    assert crypto.net_id == crypto.network_id
    assert crypto.nonce(SOURCE_IP, bytes.fromhex("0102030405060708")).hex() == (
        "5c58bf380102030405060708"
    )


def test_aes_gcm_tag8_roundtrip_and_errors() -> None:
    crypto = PiaCrypto(SSID)
    header = PiaHeader(
        dst=0x1001,
        src=0x1002,
        packet_id=7,
        nonce=bytes.fromhex("1011121314151617"),
    )
    plaintext = b"synthetic ZA transport payload"

    datagram = crypto.encrypt(plaintext, SOURCE_IP, header)
    wire_header = PiaHeader.unpack(datagram)

    assert len(wire_header.tag) == 8
    assert wire_header.tag != b"\x00" * 8
    assert wire_header.padding_size == (-len(plaintext)) % 16
    assert (len(datagram) - HEADER_SIZE) % 16 == 0
    assert crypto.decrypt(datagram, SOURCE_IP) == plaintext

    tampered = bytearray(datagram)
    tampered[-1] ^= 1
    with pytest.raises(AuthenticationError):
        crypto.decrypt(tampered, SOURCE_IP)
    with pytest.raises(AuthenticationError):
        crypto.decrypt(datagram, "169.254.21.3")

    # Byte 5 is outside GCM authentication.  A forged larger padding count is
    # therefore caught by the explicit 0xFF padding validation.
    forged_padding = bytearray(datagram)
    forged_padding[5] = (((wire_header.padding_size + 1) & 0x0F) << 4) | wire_header.flags
    with pytest.raises(PaddingError):
        crypto.decrypt(forged_padding, SOURCE_IP)


def test_crypto_accepts_an_injected_profile_key() -> None:
    custom = replace(
        ZA_PROFILE,
        name="Synthetic title",
        title_id="0123456789ABCDEF",
        game_key=b"0123456789abcdef",
    )
    from_profile = PiaCrypto(SSID, profile=custom)
    explicit = PiaCrypto(SSID, game_key=b"0123456789abcdef")
    assert from_profile.session_key == explicit.session_key
    assert from_profile.session_key != PiaCrypto(SSID).session_key


def test_compact_message_parser_carries_all_fields_forward() -> None:
    first = (
        bytes((0x1F, 0x21))
        + (3).to_bytes(2, "big")
        + bytes((10, 7, 9))
        + b"one"
    )
    second = bytes((MESSAGE_FIELD_SIZE,)) + (3).to_bytes(2, "big") + b"two"
    third = bytes((MESSAGE_FIELD_PROTOCOL, 3)) + b"rtt"

    messages = parse_messages(first + second + third)

    assert len(messages) == 3
    assert messages[0].protocol_name == "Reliable"
    assert messages[0].message_flags == 0x21
    assert messages[0].payload_size == 3
    assert messages[0].port == 7
    assert messages[0].protocol_specific == 9
    assert messages[0].serialize() == first

    assert messages[1].payload == b"two"
    assert messages[1].proto == 10
    assert messages[1].msgflags == 0x21
    assert messages[1].port == 7
    assert messages[1].protocol_specific == 9
    assert messages[1].serialize() == second

    assert messages[2].payload == b"rtt"
    assert messages[2].protocol == 3
    assert messages[2].protocol_name == "RTT"
    assert messages[2].size == 3
    assert messages[2].port == 7
    assert messages[2].protocol_specific == 9


def test_compact_message_protocol_names_cover_pia_640() -> None:
    assert protocol_name(1) == "Net"
    assert protocol_name(13) == "Session"
    assert protocol_name(18) == "Reckoning 3D"
    assert protocol_name(255) == "Unknown (255)"


def test_native_force_full_header_uses_7f_and_roundtrips() -> None:
    wire = build_message(
        10,
        b"data",
        msgflags=0x20,
        port=0,
        protocol_specific=0xFD,
        force_full_header=True,
    )
    assert wire[:7] == b"\x7f\x20\x00\x04\x0a\x00\xfd"
    message = parse_messages(wire)[0]
    assert message.flags == 0x7F
    assert message.presence_flags == 0x1F
    assert message.force_full_header is True
    assert message.payload == b"data"


def test_force_full_header_preserves_native_default_protocol_specific() -> None:
    wire = build_message(13, b"join", force_full_header=True)
    assert wire[:7] == b"\x7f\x00\x00\x04\x0d\x00\xfd"
    assert parse_messages(wire)[0].protocol_specific == 0xFD


def test_compact_message_payload_ceiling_matches_native_parser() -> None:
    body = bytes(MAX_MESSAGE_PAYLOAD_SIZE)
    assert parse_messages(build_message(10, body))[0].payload == body
    with pytest.raises(ValueError, match="cannot exceed"):
        build_message(10, body + b"x")
    oversized = b"\x06\x05\x90\x0a" + bytes(0x590)
    with pytest.raises(MessageParseError, match="accepts at most"):
        parse_messages(oversized)


def test_first_delta_header_uses_native_per_datagram_defaults() -> None:
    wire = b"\x07\x20\x00\x04\x0a" + b"data"
    message = parse_messages(wire)[0]
    assert message.msgflags == 0x20
    assert message.proto == 10
    assert message.port == 0
    assert message.protocol_specific == 0xFD

    # Reserved high bits add no fields.  With no low presence bits the other
    # native defaults are a zero-length protocol-FF message.
    default = parse_messages(b"\x20")[0]
    assert default.size == 0
    assert default.proto == 0xFF
    assert default.protocol_specific == 0xFD


@pytest.mark.parametrize(
    "data, match",
    [
        (b"\x06\x00", "truncated in payload size"),
        (b"\x06\x00\x04\x0aabc", "4-byte payload"),
    ],
)
def test_compact_message_parser_rejects_ambiguous_or_truncated_data(
    data: bytes, match: str
) -> None:
    with pytest.raises(MessageParseError, match=match):
        parse_messages(data)


def test_uncompressed_datagram_roundtrip_footer_and_json_summary() -> None:
    crypto = PiaCrypto(SSID)
    message = build_message(
        10,
        b"hello",
        msgflags=0x40,
        port=2,
        protocol_specific=9,
    )
    footer = bytes.fromhex("1234")
    header = PiaHeader(
        dst=0x2222,
        src=0x1111,
        packet_id=3,
        nonce=bytes.fromhex("2122232425262728"),
        flags=0x04,
    )

    datagram = encode_datagram(
        message,
        SOURCE_IP,
        header,
        crypto=crypto,
        footer=footer,
    )
    decoded = decode_datagram(datagram, SOURCE_IP, crypto=crypto)

    assert not decoded.compressed
    assert decoded.header.flags == 0x04
    assert decoded.footer == footer
    assert decoded.application == message + footer
    assert decoded.message_bytes == message
    assert len(decoded.messages) == 1
    assert decoded.messages[0].payload == b"hello"

    summary = json_safe_summary(decoded, payload_preview=3)
    serialized = json.dumps(summary, sort_keys=True)
    assert summary["header"]["packet_id"] == 3
    assert summary["footer_hex"] == "1234"
    assert summary["messages"][0]["protocol_name"] == "Reliable"
    assert summary["messages"][0]["payload_hex"] == "68656c"
    assert summary["messages"][0]["payload_truncated"] is True
    assert ZA_GAME_KEY.decode("ascii") not in serialized
    assert ZA_LDN_PASSPHRASE.decode("ascii") not in serialized


@pytest.mark.skipif(not HAVE_ZSTD, reason="zstandard is not installed")
def test_zstd_datagram_roundtrip() -> None:
    crypto = PiaCrypto(SSID)
    message = build_message(5, b"repeated application data " * 20)
    header = PiaHeader(
        dst=2,
        src=1,
        packet_id=42,
        nonce=b"compress",
    )

    datagram = encode_datagram(
        message,
        SOURCE_IP,
        header,
        crypto=crypto,
        footer=b"\x00\x02",
        compress=True,
    )
    decoded = decode_datagram(datagram, SOURCE_IP, crypto=crypto)

    assert decoded.header.flags & PACKET_FLAG_ZSTD
    assert decoded.compressed
    assert decoded.plaintext.startswith(bytes.fromhex("28b52ffd"))
    assert decoded.plaintext.endswith(b"\x00\x02")
    assert decoded.message_bytes == message
    assert decoded.footer == b"\x00\x02"
    assert decoded.messages[0].payload == b"repeated application data " * 20


@pytest.mark.skipif(not HAVE_ZSTD, reason="zstandard is not installed")
def test_zstd_encoder_uses_native_pia_window_descriptor() -> None:
    frame = compress_zstd(b"ZA Session Join" * 8)

    assert frame[:6] == bytes.fromhex("28b52ffd0018")
    assert decompress_zstd(frame) == b"ZA Session Join" * 8


@pytest.mark.skipif(not HAVE_ZSTD, reason="zstandard is not installed")
def test_zstd_decompression_accepts_exact_limit_and_rejects_unknown_size_bomb() -> None:
    exact = b"A" * MAX_DECOMPRESSED_MESSAGE_SIZE
    assert decompress_zstd(compress_zstd(exact)) == exact

    oversized = compress_zstd(exact + b"B")
    with pytest.raises(CompressionError):
        decompress_zstd(oversized)


@pytest.mark.skipif(not HAVE_ZSTD, reason="zstandard is not installed")
def test_decode_rejects_authenticated_zstd_frame_with_oversized_declared_output() -> None:
    import zstandard

    crypto = PiaCrypto(SSID)
    header = PiaHeader(nonce=b"zstd-bom", flags=PACKET_FLAG_ZSTD)
    compressed = zstandard.ZstdCompressor(write_content_size=True).compress(
        b"A" * (ZA_PROFILE.max_packet_size + 1)
    )
    datagram = crypto.encrypt(compressed, SOURCE_IP, header)

    with pytest.raises(CompressionError, match="declares .* limit"):
        decode_datagram(datagram, SOURCE_IP, crypto=crypto, parse=False)


def test_zstd_decompression_limit_must_be_a_positive_integer() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        decompress_zstd(b"unused", max_output_size=0)


def test_bad_zstd_marker_is_rejected_after_successful_authentication() -> None:
    crypto = PiaCrypto(SSID)
    header = PiaHeader(
        nonce=b"bad-zstd",
        flags=PACKET_FLAG_ZSTD,
    )
    datagram = crypto.encrypt(b"not a zstd frame", SOURCE_IP, header)

    with pytest.raises(CompressionError, match="magic"):
        decode_datagram(datagram, SOURCE_IP, crypto=crypto)
