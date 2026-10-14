"""Synthetic tests for :mod:`zasim.pa9` (no game or user data)."""

from __future__ import annotations

import hashlib
import inspect
import struct
import tempfile
import unittest
from pathlib import Path

from zasim.pa9 import (
    BLOCK_REGION_SIZE,
    BLOCK_SIZE,
    PARTY_SIZE,
    STORED_SIZE,
    PA9,
    PA9ChecksumError,
    PA9SanityError,
    PA9SizeError,
    calculate_checksum,
    crypt_array,
    crypt_lcrng,
    decrypt_if_encrypted,
    decrypt_pa9,
    encrypt_pa9,
    is_encrypted_pa9,
    scan_pa9,
    scan_pa9_candidates,
    shuffle_blocks,
    unshuffle_blocks,
)


def _reference_checksum(data: bytes | bytearray) -> int:
    words = struct.unpack("<160H", bytes(data[8:STORED_SIZE]))
    return sum(words) & 0xFFFF


def _synthetic_pa9(*, party: bool = True) -> bytes:
    """Build a deterministic canonical fixture without calling codec setters."""

    size = PARTY_SIZE if party else STORED_SIZE
    data = bytearray(size)
    struct.pack_into("<I", data, 0x00, 0xA5B4C3D2)
    struct.pack_into("<H", data, 0x04, 0)
    struct.pack_into("<H", data, 0x08, 25)
    struct.pack_into("<H", data, 0x0A, 0x0123)
    struct.pack_into("<H", data, 0x0C, 12345)
    struct.pack_into("<H", data, 0x0E, 54321)
    struct.pack_into("<I", data, 0x1C, 0xDEADBEEF)

    nickname = "ZA-test".encode("utf-16-le")
    data[0x58 : 0x58 + len(nickname)] = nickname
    data[0x58 + len(nickname) : 0x58 + len(nickname) + 2] = b"\0\0"
    ot_name = "CODEX".encode("utf-16-le")
    data[0xF8 : 0xF8 + len(ot_name)] = ot_name
    data[0xF8 + len(ot_name) : 0xF8 + len(ot_name) + 2] = b"\0\0"

    # Deterministic non-zero content in each canonical block exercises both
    # word encryption and block movement while keeping the string terminators.
    for offset in range(0x72, 0xA0):
        data[offset] = (offset * 29 + 7) & 0xFF
    for offset in range(0xB0, 0xF0):
        data[offset] = (offset * 17 + 3) & 0xFF
    for offset in range(0x120, STORED_SIZE):
        data[offset] = (offset * 11 + 5) & 0xFF
    if party:
        data[STORED_SIZE:PARTY_SIZE] = bytes(
            [73, 0, 201, 0, 143, 0, 127, 0, 166, 0, 155, 0, 149, 0, 0, 0]
        )

    struct.pack_into("<H", data, 6, _reference_checksum(data))
    return bytes(data)


class PA9PrimitiveTests(unittest.TestCase):
    def test_constants_match_pa9_container_sizes(self) -> None:
        self.assertEqual(STORED_SIZE, 0x148)
        self.assertEqual(PARTY_SIZE, 0x158)
        self.assertEqual(BLOCK_REGION_SIZE, 4 * 0x50)

    def test_lcrng_known_vector_and_involution(self) -> None:
        plain = bytes(range(16))
        encrypted = crypt_lcrng(plain, 0x12345678)
        self.assertEqual(encrypted.hex(), "710ae8878edce6f38c2f3d93a09c8b0a")
        self.assertEqual(crypt_lcrng(encrypted, 0x12345678), plain)
        self.assertEqual(crypt_array(plain, 0x12345678), encrypted)

    def test_lcrng_rejects_bad_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "even"):
            crypt_lcrng(b"odd", 0)
        with self.assertRaisesRegex(ValueError, "seed"):
            crypt_lcrng(b"\0\0", 1 << 32)
        with self.assertRaisesRegex(TypeError, "bytes-like"):
            crypt_lcrng("not bytes", 0)  # type: ignore[arg-type]

    def test_shuffle_known_layout_and_all_values_round_trip(self) -> None:
        canonical = b"".join(bytes([index]) * BLOCK_SIZE for index in range(4))

        # Direct storage orders corresponding to PKHeX's BlockPositionInvert.
        expected_storage_orders = (
            (0, 1, 2, 3),
            (0, 1, 3, 2),
            (0, 2, 1, 3),
            (0, 2, 3, 1),
            (0, 3, 1, 2),
            (0, 3, 2, 1),
            (1, 0, 2, 3),
            (1, 0, 3, 2),
            (1, 2, 0, 3),
            (1, 2, 3, 0),
            (1, 3, 0, 2),
            (1, 3, 2, 0),
            (2, 0, 1, 3),
            (2, 0, 3, 1),
            (2, 1, 0, 3),
            (2, 1, 3, 0),
            (2, 3, 0, 1),
            (2, 3, 1, 0),
            (3, 0, 1, 2),
            (3, 0, 2, 1),
            (3, 1, 0, 2),
            (3, 1, 2, 0),
            (3, 2, 0, 1),
            (3, 2, 1, 0),
        )

        # Shuffle value 4 decrypts with [0,2,3,1], so encrypted storage is
        # inverse order [0,3,1,2].
        shuffled = shuffle_blocks(canonical, 4)
        self.assertEqual(
            [shuffled[i * BLOCK_SIZE] for i in range(4)], [0, 3, 1, 2]
        )
        self.assertEqual(unshuffle_blocks(shuffled, 4), canonical)

        for value in range(32):
            with self.subTest(value=value):
                shuffled = shuffle_blocks(canonical, value)
                self.assertEqual(
                    tuple(shuffled[i * BLOCK_SIZE] for i in range(4)),
                    expected_storage_orders[value % 24],
                )
                self.assertEqual(
                    unshuffle_blocks(shuffled, value),
                    canonical,
                )

    def test_shuffle_rejects_wrong_region_or_value(self) -> None:
        with self.assertRaises(PA9SizeError):
            shuffle_blocks(bytes(BLOCK_REGION_SIZE - 1), 0)
        with self.assertRaisesRegex(ValueError, "shuffle_value"):
            unshuffle_blocks(bytes(BLOCK_REGION_SIZE), 32)

    def test_checksum_known_pattern_and_excludes_party_tail(self) -> None:
        stored = bytearray(STORED_SIZE)
        stored[8:] = bytes(
            ((index * 37 + 11) & 0xFF) for index in range(STORED_SIZE - 8)
        )
        self.assertEqual(calculate_checksum(stored), 0x2FC0)

        party = stored + bytearray([0xFF] * (PARTY_SIZE - STORED_SIZE))
        self.assertEqual(calculate_checksum(party), 0x2FC0)


class PA9RecordTests(unittest.TestCase):
    def test_new_record_uses_pkhex_no_affixed_ribbon_sentinel(self) -> None:
        record = PA9()

        self.assertEqual(record.data[0xD4], 0xFF)
        self.assertEqual(record.checksum, 0x00FF)
        self.assertTrue(record.valid)

    def test_decoded_record_fields(self) -> None:
        record = PA9(_synthetic_pa9())
        self.assertEqual(record.ec, 0xA5B4C3D2)
        self.assertEqual(record.encryption_constant, record.ec)
        self.assertEqual(record.species_internal, 25)
        self.assertEqual(record.species, 25)
        self.assertEqual(record.item, 0x0123)
        self.assertEqual(record.held_item, 0x0123)
        self.assertEqual(record.tid, 12345)
        self.assertEqual(record.sid, 54321)
        self.assertEqual(record.pid, 0xDEADBEEF)
        self.assertEqual(record.nickname, "ZA-test")
        self.assertTrue(record.is_party)
        self.assertTrue(record.checksum_valid)
        self.assertTrue(record.checksum_ok)
        self.assertTrue(record.valid)

    def test_field_setters_and_utf16_nickname(self) -> None:
        record = PA9(_synthetic_pa9())
        record.ec = 0x10203040
        record.species_internal = 999
        record.item = 88
        record.tid = 1
        record.sid = 2
        record.pid = 3
        record.nickname = "ミアレ😀"

        self.assertEqual(record.encryption_constant, 0x10203040)
        self.assertEqual(record.species, 999)
        self.assertEqual(record.held_item, 88)
        self.assertEqual((record.tid, record.sid, record.pid), (1, 2, 3))
        self.assertEqual(record.nickname, "ミアレ😀")
        self.assertFalse(record.checksum_valid)
        record.update_checksum()
        self.assertTrue(record.checksum_valid)

        # Ten ASCII units + one supplementary code point uses exactly the
        # twelve-code-unit limit; the following Z is truncated.
        record.nickname = "ABCDEFGHIJ😀Z"
        self.assertEqual(record.nickname, "ABCDEFGHIJ😀")

        # Truncation must not retain a lone high surrogate when the pair would
        # cross the twelve-code-unit boundary.
        record.nickname = "ABCDEFGHIJK😀"
        self.assertEqual(record.nickname, "ABCDEFGHIJK")

    def test_extended_editor_field_accessors(self) -> None:
        record = PA9()
        record.experience = 1_234_567
        record.ability = 321
        record.ability_number = 4
        record.nature = 3
        record.stat_nature = 13
        record.fateful_encounter = True
        record.gender = 2
        record.is_alpha = True
        record.form = 7
        for name, value in {
            "ev_hp": 1,
            "ev_atk": 2,
            "ev_def": 3,
            "ev_spa": 4,
            "ev_spd": 5,
            "ev_spe": 6,
            "move1": 10,
            "move2": 20,
            "move3": 30,
            "move4": 40,
            "move1_pp": 11,
            "move2_pp": 12,
            "move3_pp": 13,
            "move4_pp": 14,
            "move1_pp_ups": 0,
            "move2_pp_ups": 1,
            "move3_pp_ups": 2,
            "move4_pp_ups": 3,
            "relearn_move1": 50,
            "relearn_move2": 60,
            "relearn_move3": 70,
            "relearn_move4": 80,
            "iv_hp": 31,
            "iv_atk": 30,
            "iv_def": 29,
            "iv_spa": 28,
            "iv_spd": 27,
            "iv_spe": 26,
        }.items():
            setattr(record, name, value)
        record.is_egg = True
        record.is_nicknamed = True
        record.version = 44
        record.language = 2
        record.original_trainer_name = "ミアレ"
        record.ot_friendship = 200
        record.met_year = 26
        record.met_month = 8
        record.met_day = 30
        record.obedience_level = 50
        record.egg_location = 100
        record.met_location = 200
        record.ball = 4
        record.met_level = 35
        record.ot_gender = 1
        record.stat_level = 50
        record.stat_hp_current = 111
        record.stat_hp_max = 222
        record.stat_atk = 101
        record.stat_def = 102
        record.stat_spa = 103
        record.stat_spd = 104
        record.stat_spe = 105

        self.assertEqual(record.exp, 1_234_567)
        self.assertEqual(record.stat_alignment, 13)
        self.assertEqual((record.gender, record.form, record.alpha), (2, 7, True))
        self.assertEqual(
            (record.move1, record.move4, record.move4_pp, record.move4_pp_ups),
            (10, 40, 14, 3),
        )
        self.assertEqual(
            (record.iv_hp, record.iv_atk, record.iv_def, record.iv_spa, record.iv_spd, record.iv_spe),
            (31, 30, 29, 28, 27, 26),
        )
        self.assertTrue(record.is_egg)
        self.assertTrue(record.is_nicknamed)
        self.assertEqual(record.ot_name, "ミアレ")
        self.assertEqual(
            (record.stat_level, record.stat_hp_current, record.stat_hp_max),
            (50, 111, 222),
        )
        self.assertEqual(
            (record.stat_atk, record.stat_def, record.stat_spa, record.stat_spd, record.stat_spe),
            (101, 102, 103, 104, 105),
        )
        record.update_checksum()
        self.assertTrue(record.checksum_valid)

    def test_party_stat_setters_reject_stored_record(self) -> None:
        record = PA9(_synthetic_pa9(party=False))
        with self.assertRaisesRegex(PA9SizeError, "party PA9"):
            record.stat_level = 50
        with self.assertRaisesRegex(PA9SizeError, "party PA9"):
            record.stat_atk = 100

    def test_setters_reject_out_of_range_and_wrong_types(self) -> None:
        record = PA9(_synthetic_pa9())
        with self.assertRaisesRegex(ValueError, "species_internal"):
            record.species_internal = 0x10000
        with self.assertRaisesRegex(ValueError, "pid"):
            record.pid = -1
        with self.assertRaisesRegex(TypeError, "nickname"):
            record.nickname = b"bytes"  # type: ignore[assignment]

    def test_stored_and_party_encrypt_decrypt_round_trip(self) -> None:
        for party in (False, True):
            with self.subTest(party=party):
                canonical = _synthetic_pa9(party=party)
                encrypted = encrypt_pa9(canonical)
                self.assertEqual(len(encrypted), len(canonical))
                self.assertNotEqual(encrypted[8:STORED_SIZE], canonical[8:STORED_SIZE])
                self.assertTrue(is_encrypted_pa9(encrypted))
                self.assertFalse(is_encrypted_pa9(canonical))
                self.assertEqual(decrypt_pa9(encrypted), canonical)
                self.assertEqual(decrypt_if_encrypted(encrypted), canonical)
                self.assertEqual(decrypt_if_encrypted(canonical), canonical)

                decoded = PA9(encrypted)
                self.assertTrue(decoded.was_encrypted)
                self.assertEqual(decoded.data, canonical)

    def test_party_tail_uses_fresh_lcrng_stream(self) -> None:
        canonical = _synthetic_pa9(party=True)
        encrypted = encrypt_pa9(canonical)
        self.assertEqual(
            encrypted[STORED_SIZE:PARTY_SIZE],
            crypt_lcrng(canonical[STORED_SIZE:PARTY_SIZE], 0xA5B4C3D2),
        )

    def test_synthetic_encrypted_vector_is_stable(self) -> None:
        encrypted = encrypt_pa9(_synthetic_pa9(party=False))
        # Generated independently by compiling the repository's unmodified
        # PKHeX.Core/PKM/Util/PokeCrypto.cs and calling Encrypt8 on this fixture.
        self.assertEqual(
            hashlib.sha256(encrypted).hexdigest(),
            "307309527ab00ac1c05a54be764566906c4e691c4e722c23adbb59e5f1a38da7",
        )

    def test_validation_errors_are_specific(self) -> None:
        with self.assertRaisesRegex(PA9SizeError, "0x148"):
            PA9(bytes(10))

        bad_sanity = bytearray(_synthetic_pa9())
        struct.pack_into("<H", bad_sanity, 4, 7)
        with self.assertRaisesRegex(PA9SanityError, "0x0007"):
            PA9(bad_sanity, encrypted=False)

        bad_checksum = bytearray(_synthetic_pa9())
        bad_checksum[0x20] ^= 1
        with self.assertRaisesRegex(PA9ChecksumError, "mismatch"):
            PA9(bad_checksum, encrypted=False)

        editable = PA9(bad_checksum, encrypted=False, validate=False)
        self.assertFalse(editable.valid)
        editable.update_checksum()
        editable.validate()

    def test_export_can_change_container_size(self) -> None:
        stored = PA9(_synthetic_pa9(party=False))
        self.assertEqual(len(stored.to_bytes()), STORED_SIZE)
        widened = stored.to_bytes(party=True)
        self.assertEqual(len(widened), PARTY_SIZE)
        self.assertEqual(widened[STORED_SIZE:], bytes(PARTY_SIZE - STORED_SIZE))

        party = PA9(_synthetic_pa9(party=True))
        self.assertEqual(len(party.to_bytes(party=False)), STORED_SIZE)

    def test_file_round_trip_and_suffix_encryption(self) -> None:
        original = PA9(_synthetic_pa9(party=True))
        with tempfile.TemporaryDirectory() as directory:
            decrypted_path = Path(directory, "synthetic.pa9")
            encrypted_path = Path(directory, "synthetic.ea9")
            self.assertEqual(original.save(decrypted_path), decrypted_path)
            original.save(encrypted_path)

            self.assertFalse(is_encrypted_pa9(decrypted_path.read_bytes()))
            self.assertTrue(is_encrypted_pa9(encrypted_path.read_bytes()))
            self.assertEqual(PA9.from_file(decrypted_path).data, original.data)
            self.assertEqual(PA9.from_file(encrypted_path).data, original.data)

            wrong_path = Path(directory, "wrong.pa9")
            wrong_path.write_bytes(b"short")
            with self.assertRaisesRegex(PA9SizeError, "wrong.pa9"):
                PA9.from_file(wrong_path)


class PA9ScannerTests(unittest.TestCase):
    def test_scanner_tries_alternate_encryption_state_when_heuristic_misses(self) -> None:
        # Shuffle value 7 puts canonical blocks 0 and 2 at the two storage
        # words used by PKHeX's encryption heuristic.  Choose their plaintext
        # words so that encryption produces zero at both heuristic locations.
        for party in (False, True):
            with self.subTest(party=party):
                canonical = bytearray(_synthetic_pa9(party=party))
                struct.pack_into("<I", canonical, 0, 7 << 13)
                struct.pack_into("<H", canonical, 6, _reference_checksum(canonical))
                first = encrypt_pa9(canonical)
                for target, source_block in ((0x70, 0), (0x110, 2)):
                    source = 8 + (source_block * BLOCK_SIZE) + 0x18
                    old_word = struct.unpack_from("<H", canonical, source)[0]
                    cipher_word = struct.unpack_from("<H", first, target)[0]
                    struct.pack_into("<H", canonical, source, old_word ^ cipher_word)
                struct.pack_into("<H", canonical, 6, _reference_checksum(canonical))
                encrypted = encrypt_pa9(canonical)

                self.assertFalse(is_encrypted_pa9(encrypted))
                forced = PA9(encrypted, encrypted=True)
                self.assertEqual(forced.species_internal, 25)

                candidates = scan_pa9_candidates(
                    encrypted, record_size=len(canonical)
                )
                self.assertEqual(len(candidates), 1)
                self.assertTrue(candidates[0].encrypted)
                self.assertEqual(candidates[0].pa9.data, bytes(canonical))

    def test_scans_decrypted_and_encrypted_stored_records(self) -> None:
        canonical = _synthetic_pa9(party=False)
        encrypted = encrypt_pa9(canonical)
        second = bytearray(canonical)
        struct.pack_into("<H", second, 0x08, 133)
        struct.pack_into("<H", second, 6, _reference_checksum(second))

        first_offset = 17
        second_offset = first_offset + STORED_SIZE + 23
        payload = b"prefix-junk-bytes" + encrypted + bytes([0xCC]) * 23 + bytes(second)
        self.assertEqual(len(b"prefix-junk-bytes"), first_offset)

        candidates = scan_pa9_candidates(payload)
        self.assertEqual([candidate.offset for candidate in candidates], [first_offset, second_offset])
        self.assertEqual([candidate.encrypted for candidate in candidates], [True, False])
        self.assertEqual(candidates[0].pa9.species_internal, 25)
        self.assertEqual(candidates[1].record.species_internal, 133)
        self.assertEqual(candidates[0].size, STORED_SIZE)
        self.assertEqual(
            [candidate.offset for candidate in scan_pa9(payload)],
            [candidate.offset for candidate in candidates],
        )

    def test_party_scan_and_bounds(self) -> None:
        record = encrypt_pa9(_synthetic_pa9(party=True))
        payload = b"xyz" + record + b"tail"
        result = scan_pa9_candidates(
            payload,
            record_size=PARTY_SIZE,
            start=3,
            end=3 + PARTY_SIZE,
            max_candidates=1,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].offset, 3)
        self.assertTrue(result[0].pa9.is_party)

    def test_scanner_rejects_corruption_and_empty_slots(self) -> None:
        corrupted = bytearray(_synthetic_pa9(party=False))
        corrupted[0x30] ^= 0x80
        self.assertEqual(scan_pa9_candidates(corrupted), [])
        self.assertEqual(scan_pa9_candidates(bytes(STORED_SIZE)), [])

        empties = scan_pa9_candidates(bytes(STORED_SIZE), require_species=False)
        self.assertEqual(len(empties), 1)
        self.assertEqual(empties[0].offset, 0)

    def test_scanner_arguments_are_checked(self) -> None:
        payload = bytes(STORED_SIZE)
        with self.assertRaises(PA9SizeError):
            scan_pa9_candidates(payload, record_size=123)
        with self.assertRaisesRegex(ValueError, "bounds"):
            scan_pa9_candidates(payload, start=2, end=1)
        with self.assertRaisesRegex(ValueError, "alignment"):
            scan_pa9_candidates(payload, alignment=0)
        with self.assertRaisesRegex(ValueError, "max_candidates"):
            scan_pa9_candidates(payload, max_candidates=0)


class PA9DocumentationTests(unittest.TestCase):
    def test_public_api_has_docstrings(self) -> None:
        public = (
            PA9,
            calculate_checksum,
            crypt_lcrng,
            decrypt_pa9,
            encrypt_pa9,
            is_encrypted_pa9,
            scan_pa9_candidates,
            shuffle_blocks,
            unshuffle_blocks,
        )
        for value in public:
            with self.subTest(value=value):
                self.assertTrue(inspect.getdoc(value))


if __name__ == "__main__":
    unittest.main()
