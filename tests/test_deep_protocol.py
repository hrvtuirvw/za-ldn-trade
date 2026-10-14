"""Bounded deterministic decoder/reordering/durability stress (offline only)."""
from __future__ import annotations

from dataclasses import replace
import random

import pytest
import zstandard

from zasim import pia, peer_setup, session_update, sync_save, trade_protocol
from zasim.pa9 import PA9
from zasim.reliable import ReliableFrame, ReliableReassembler, parse_bulk_ack, parse_reliable
from zasim.user_data import UserData


def _decoder(name, raw):
    if name == "reliable":
        return parse_reliable(raw)
    if name == "bulk-ack":
        return parse_bulk_ack(raw)
    if name == "compact-pia":
        return pia.parse_messages(raw)
    if name == "pia-header":
        return pia.PiaHeader.unpack(raw)
    if name == "session-update":
        return session_update.parse_update(raw)
    if name == "peer-setup":
        return peer_setup.decode(raw)
    if name == "user-data":
        return UserData.decode(raw)
    if name == "sync-save":
        return sync_save.decode(raw)
    if name.startswith("trade-"):
        return trade_protocol.decode_command(0x100 + int(name[-1]), raw)
    raise AssertionError(name)


DECODERS = ["reliable", "bulk-ack", "compact-pia", "pia-header", "session-update", "peer-setup", "user-data", "sync-save"] + [f"trade-{i}" for i in range(5)]


@pytest.mark.parametrize("decoder", DECODERS)
def test_seeded_malformed_decoder_inputs_have_only_controlled_errors(decoder):
    rng = random.Random(0x8_1_20261006)
    corpus = [b"", bytes(8), bytes(range(256)), b"\xff" * 1600]
    corpus += [rng.randbytes(rng.randrange(0, 1601)) for _ in range(256)]
    # Length boundaries, truncations, and structure-like prefixes supplement noise.
    for size in (1, 2, 3, 4, 5, 7, 8, 9, 15, 16, 28, 29, 30, 127, 255, 328, 344, 1200):
        corpus.extend([bytes(size), b"\xff" * size, b"\xb9\x01" + bytes(size)])
    for raw in corpus:
        try:
            _decoder(decoder, raw)
        except (ValueError, TypeError):
            pass  # Declared validation errors, not IndexError/assertion/recursion.


@pytest.mark.parametrize("seed", list(range(20)))
def test_reassembly_randomized_loss_reorder_duplicates_and_ack_progress(seed):
    rng = random.Random(seed)
    start = (0xFFF0 + seed) & 0xFFFF
    rx = ReliableReassembler(next_sequence=start)
    frames, expected = [], []
    sequence = start
    for _ in range(10):
        parts = [rng.randbytes(rng.randrange(0, 65)) for _ in range(rng.randrange(1, 5))]
        expected.append(b"".join(parts))
        for index, part in enumerate(parts):
            flags = 1 | (2 if index == 0 else 0) | (4 if index == len(parts) - 1 else 0)
            frames.append(ReliableFrame(flags, sequence, rng.randrange(65536), part))
            sequence = (sequence + 1) & 0xFFFF
    order = list(range(1, len(frames))); rng.shuffle(order)
    for index in order:
        assert rx.feed(frames[index]) == ()
        assert rx.feed(replace(frames[index], lowest_pending_ack=rng.randrange(65536))) == ()
    assert len(rx.pending_sequences) == len(frames) - 1
    assert [item.payload for item in rx.feed(frames[0])] == expected
    assert rx.pending_sequences == () and rx.next_sequence == sequence
    rng.shuffle(frames)
    for frame in frames:
        assert rx.feed(replace(frame, lowest_pending_ack=rng.randrange(65536))) == ()


@pytest.mark.parametrize("content_size", [False, True])
@pytest.mark.parametrize("size", [256, 257, 65536])
def test_zstandard_declared_and_unknown_size_are_bounded(content_size, size):
    raw = b"A" * size
    compressed = zstandard.ZstdCompressor(write_content_size=content_size).compress(raw)
    if size > 256:
        with pytest.raises(pia.CompressionError):
            pia.decompress_zstd(compressed, max_output_size=256)
    else:
        assert pia.decompress_zstd(compressed, max_output_size=256) == raw


@pytest.mark.parametrize("failure", ["prepare", "persist", "commit", "finish", None])
@pytest.mark.parametrize("throws", [False, True])
def test_save_sync_cannot_announce_completion_after_nondurable_callbacks(failure, throws):
    calls = []
    def operation(name):
        def run():
            calls.append(name)
            if failure == name:
                if throws:
                    raise OSError("synthetic durability failure")
                return False
            return True
        return run
    peer = sync_save.SyncSavePeer(**{name: operation(name) for name in ("prepare", "persist", "commit", "finish")})
    now = 0.0
    try:
        peer.begin(now=now)
        peer.drain(now=now)
        for phase in sync_save.PHASES:
            now += 1
            msg = sync_save.encode(sync_save.Message(sync_save.Command.SYNCED, phase))
            peer.receive(msg, now=now)
            # Old duplicate notifications never repeat durable I/O.
            peer.receive(msg, now=now)
            peer.drain(now=now + 0.1)
    except (OSError, sync_save.SyncSaveError):
        assert failure is not None
        assert peer.failed and not peer.complete
        with pytest.raises(sync_save.SyncSaveError):
            peer.drain(now=10)
        with pytest.raises(sync_save.SyncSaveError):
            peer.begin(now=10)
    else:
        assert failure is None and peer.complete and not peer.failed
    names = ["prepare", "persist", "commit", "finish"]
    assert calls == (names if failure is None else names[:names.index(failure) + 1])


@pytest.mark.parametrize("encrypted", [False, True])
def test_pa9_roundtrip_preserves_every_synthetic_byte(encrypted):
    rng = random.Random(0x81)
    for size in (PA9.STORED_SIZE, PA9.PARTY_SIZE):
        for _ in range(80):
            raw = bytearray(rng.randbytes(size));raw[4:6] = b"\x00\x00"
            record = PA9(raw, encrypted=False, validate=False)
            record.update_checksum()
            packed = record.to_bytes(encrypted=encrypted)
            restored = PA9(packed, encrypted=encrypted)
            assert restored.data == record.data


@pytest.mark.parametrize("compressed", [False, True])
def test_authentication_rejects_every_ciphertext_byte_mutation(compressed):
    ssid = bytes(range(16))
    crypto = pia.PiaCrypto(ssid)
    compact = pia.build_message(10, ReliableFrame(7, 5, 4, b"synthetic-data").pack())
    header = pia.PiaHeader(src=1, dst=2, packet_id=1, nonce=b"\x01" * 8)
    wire = pia.encode_datagram(compact, "169.254.1.2", header, crypto=crypto, compress=compressed)
    assert pia.decode_datagram(wire, "169.254.1.2", crypto=crypto).message_bytes == compact
    for offset in range(pia.PAYLOAD_OFFSET, len(wire)):
        changed = bytearray(wire); changed[offset] ^= 1
        with pytest.raises(pia.AuthenticationError):
            pia.decode_datagram(changed, "169.254.1.2", crypto=crypto)
    with pytest.raises(pia.AuthenticationError):
        pia.decode_datagram(wire, "169.254.1.3", crypto=crypto)
    with pytest.raises(pia.AuthenticationError):
        pia.decode_datagram(wire, "169.254.1.2", ssid=bytes(reversed(ssid)))
