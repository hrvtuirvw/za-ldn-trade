"""Save-independent PA9 creation and real party-stat calculations.

Public PKHeX-derived data/rules; see THIRD_PARTY_NOTICES.md. No ROM, save,
network, executable, SDK or PKHeX installation is read by this module.
This is NOT a full legality checker. Custom encounters are explicitly drafts.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
import hashlib
from importlib.resources import files
import json
import secrets
from typing import Mapping

from .pa9 import PA9, PARTY_SIZE

STAT_NAMES = ("hp", "atk", "def", "spa", "spd", "spe")
LANGUAGES = (1, 2, 3, 4, 5, 7, 8, 9, 10, 11)
SOURCE_COMMIT = "e15d2467b32da7bc26ce7cc8e5c4ede32740e20a"
# Six player-owned gifts, not fixed-NPC gifts or a universal encounter solver.
STARTERS = {
    152: (30026, 128, (33, 39)),
    498: (30026, 128, (33, 39)),
    158: (30026, 128, (33, 43)),
    1: (30027, None, ()),
    4: (30027, None, ()),
    7: (30027, None, ()),
}


def _int(value: int, minimum: int, maximum: int, name: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


@lru_cache(maxsize=1)
def _data() -> dict:
    resource = files("zasim").joinpath("data/za_creation.json")
    raw = resource.read_bytes()
    provenance = json.loads(files("zasim").joinpath("data/za_creation_provenance.json").read_text(encoding="utf-8"))
    if hashlib.sha256(raw).hexdigest() != provenance["data_sha256"]:
        raise ValueError("bundled ZA creation data failed its integrity check")
    result = json.loads(raw)
    if result["schema"] != 1 or result["source_commit"] != SOURCE_COMMIT:
        raise ValueError("unsupported ZA creation data revision")
    return result


def species_catalog() -> tuple[dict, ...]:
    """Available National Dex IDs/forms; battle-only forms are not creatable."""
    return tuple(dict(p) for p in sorted(_data()["personal"].values(), key=lambda p: (p["species"], p["form"])) if not p["battle_only"])


def personal_info(species: int, form: int = 0) -> Mapping:
    _int(species, 1, 1025, "National Dex species")
    _int(form, 0, 255, "form")
    p = _data()["personal"].get(f"{species}:{form}")
    if p is None or p["battle_only"]:
        raise ValueError(f"species/form {species}:{form} is absent or battle-only in the bundled ZA table")
    return p


def personal_for_record(record: PA9) -> Mapping:
    # The editor's species field is INTERNAL. Creation accepts NATIONAL IDs.
    species, form = record.species_internal, record.form
    for p in _data()["personal"].values():
        if p["internal_id"] == species and p["form"] == form:
            return personal_info(p["species"], p["form"])
    raise ValueError("no ZA personal data for this internal species/form; cannot calculate stats")


def experience_for_level(level: int, growth: int) -> int:
    _int(level, 1, 100, "level")
    _int(growth, 0, 5, "growth")
    return _data()["growth"][growth][level - 1]


def level_for_experience(experience: int, growth: int) -> int:
    _int(growth, 0, 5, "growth")
    table = _data()["growth"][growth]
    _int(experience, 0, table[-1], "experience")
    return bisect_right(table, experience)


def calculate_stats(record: PA9) -> dict[str, int]:
    """Calculate, never trust the cached level. Includes mint and Hyper Training."""
    p = personal_for_record(record)
    level = level_for_experience(record.experience, p["growth"])
    _int(record.stat_nature, 0, 24, "stat_nature")
    evs = [getattr(record, "ev_" + n) for n in STAT_NAMES]
    for value in evs:
        _int(value, 0, 252, "EV")
    if sum(evs) > 510:
        raise ValueError("EV total must not exceed 510 for stat calculation")
    hyper = record.data[0x126]
    if hyper & ~0x3F:
        raise ValueError("invalid Hyper Training flags")
    result = {"stat_level": level}
    # Nature order is ATK, DEF, SPE, SPA, SPD; hyper flags use speed LAST.
    nature_order = ("atk", "def", "spe", "spa", "spd")
    up, down = divmod(record.stat_nature, 5)
    for i, name in enumerate(STAT_NAMES):
        iv = 31 if hyper & (1 << i) else getattr(record, "iv_" + name)
        base = p["stats"][i]
        value = (2 * base + iv + evs[i] // 4) * level // 100
        if name == "hp":
            value = 1 if base == 1 else value + level + 10
        else:
            value += 5
            if up != down:
                if name == nature_order[up]:
                    value = value * 110 // 100
                elif name == nature_order[down]:
                    value = value * 90 // 100
        result["stat_hp_max" if name == "hp" else "stat_" + name] = value
    return result


def recalculate_party(record: PA9, *, level: int | None = None, promote: bool = False) -> PA9:
    """Return a calculated, healed copy. Stored->party requires explicit promote.

    Only the cache/current HP/status, optional EXP and checksum are changed.
    This does not repair encounters, moves, abilities or RNG correlations.
    """
    record.validate()
    if not record.is_party and not promote:
        raise ValueError("stored PA9 needs explicit --party / party conversion")
    raw = bytearray(record.data)
    if not record.is_party:
        raw.extend(bytes(PARTY_SIZE - len(raw)))
    candidate = PA9(raw, encrypted=False)
    if level is not None:
        p = personal_for_record(candidate)
        candidate.experience = experience_for_level(level, p["growth"])
    stats = calculate_stats(candidate)
    for name, value in stats.items():
        setattr(candidate, name, value)
    candidate.stat_hp_current = candidate.stat_hp_max
    raw = bytearray(candidate.data)
    raw[0x90:0x94] = bytes(4)  # ResetPartyStats heals status too.
    result = PA9(raw, encrypted=False, validate=False)
    result.update_checksum()
    result.validate()
    return result


class _Xoroshiro:
    """PKHeX Xoroshiro128Plus.NextInt rejection sampler (not Python's RNG)."""
    def __init__(self, seed: int):
        self.s0, self.s1 = seed, 0x82A2B175229D6A5B

    def next(self) -> int:
        mask = (1 << 64) - 1
        a, b = self.s0, self.s1
        result = (a + b) & mask
        b ^= a
        self.s0 = (((a << 24) | (a >> 40)) ^ b ^ (b << 16)) & mask
        self.s1 = ((b << 37) | (b >> 27)) & mask
        return result

    def integer(self, maximum: int = 0xFFFFFFFF) -> int:
        mask = (1 << (maximum - 1).bit_length()) - 1
        while True:
            value = self.next() & mask
            if value < maximum:
                return value


def _shiny(pid: int, trainer: int) -> bool:
    return ((pid >> 16) ^ (pid & 0xFFFF) ^ (trainer >> 16) ^ (trainer & 0xFFFF)) < 16


def _pinga(record: PA9, p: Mapping, seed: int, *, flawless: int, scale: int | None) -> PA9:
    rng = _Xoroshiro(seed)
    record.ec = rng.integer()
    fake_trainer, pid = rng.integer(), rng.integer()
    if _shiny(pid, fake_trainer):
        pid ^= 0x10000000
    if _shiny(pid, record.tid | (record.sid << 16)):
        pid ^= 0x10000000
    record.pid = pid
    ivs = [-1] * 6
    for _ in range(flawless):
        while True:
            index = rng.integer(6)
            if ivs[index] == -1:
                ivs[index] = 31
                break
    for i in range(6):
        if ivs[i] == -1:
            ivs[i] = rng.integer(32)
    # Player starter gifts use ReApplyIVs; the consumed sequence above is still
    # required for ability/gender/nature/scale. These IVs meet that unconstrained
    # re-application (>=3 flawless); no claim of a universal encounter solver.
    for name, value in zip(STAT_NAMES, ivs):
        setattr(record, "iv_" + name, value)
    ability = rng.integer(2)
    record.ability = p["abilities"][ability]
    record.ability_number = 1 << ability
    ratio = p["gender"]
    if ratio in (0, 254, 255):
        record.gender = {0: 0, 254: 1, 255: 2}[ratio]
    else:
        threshold = {31: 12, 63: 25, 127: 50, 191: 75, 225: 89}[ratio]
        record.gender = int(rng.integer(100) < threshold)
    record.nature = record.stat_nature = rng.integer(25)
    raw = bytearray(record.data)
    raw[0x4A] = rng.integer(129) + rng.integer(128) if scale is None else scale
    raw[0xD4] = 0xFF  # No affixed ribbon; zero would incorrectly select a ribbon.
    return PA9(raw, encrypted=False, validate=False)


def encounter_moves(p: Mapping, level: int) -> tuple[int, ...]:
    moves: list[int] = []
    for learned, move in reversed(p["learn"]):
        if 0 < learned <= level and move not in moves:
            moves.append(move)
            if len(moves) == 4:
                break
    return tuple(moves)


def _moves_and_plus(record: PA9, p: Mapping, level: int, moves: tuple[int, ...]) -> PA9:
    if not 1 <= len(moves) <= 4 or len(set(moves)) != len(moves) or 0 in moves:
        raise ValueError("provide 1..4 distinct, nonzero move IDs")
    allowed = {m for lv, m in p["learn"] if lv <= level} | set(p["tm"])
    pp = _data()["pp"]
    for i, move in enumerate(moves, 1):
        _int(move, 1, len(pp) - 1, "move")
        if move not in allowed:
            raise ValueError(f"move {move} is not in this species/form's ZA level/TM pool")
        setattr(record, f"move{i}", move)
    # PKHeX/ZA HealPP initializes all FOUR slots, including move 0 (PP=35).
    for i in range(1, 5):
        setattr(record, f"move{i}_pp", pp[getattr(record, f"move{i}")])
    raw = bytearray(record.data)
    for lv, move in p["plus"]:
        if lv <= level:
            index = _data()["plus_moves"].index(move)
            offset = 0xD6 + index // 8 if index < 264 else 0x94 + (index - 264) // 8
            raw[offset] |= 1 << (index % 8)
    result = PA9(raw, encrypted=False, validate=False)
    result.update_checksum()
    return result


@dataclass(frozen=True)
class CreateRequest:
    species: int  # National, NOT the editor's internal ID.
    ot_name: str
    tid: int
    sid: int
    encounter: str = "starter"
    form: int = 0
    level: int | None = None
    language: int = 1
    ot_gender: int = 0
    met_date: date | None = None
    met_location: int | None = None
    met_level: int | None = None
    ball: int = 4
    nickname: str | None = None
    moves: tuple[int, ...] | None = None
    seed: int | None = None


def create_pa9(request: CreateRequest) -> PA9:
    """Create a new full party record without a template or save.

    starter: six player gifts with fixed encounter rules and correlated RNG.
    custom: explicit encounter metadata; modeled stats/moves, legality unknown.
    """
    from .pa9_editor import FIELD_SPECS, parse_editor_value

    p = personal_info(request.species, request.form)
    if request.encounter not in ("starter", "custom"):
        raise ValueError("encounter must be starter or custom")
    if request.language not in LANGUAGES or type(request.language) is not int:
        raise ValueError("unsupported language ID")
    _int(request.tid, 0, 65535, "TID16")
    _int(request.sid, 0, 65535, "SID16")
    _int(request.ot_gender, 0, 1, "OT gender")
    parse_editor_value(FIELD_SPECS["original_trainer_name"], request.ot_name)
    if not request.ot_name.strip() or any(ord(c) < 32 for c in request.ot_name):
        raise ValueError("OT name must be nonempty and contain no control characters")
    if request.nickname is not None:
        parse_editor_value(FIELD_SPECS["nickname"], request.nickname)
        if not request.nickname.strip() or any(ord(c) < 32 for c in request.nickname):
            raise ValueError("nickname must be nonempty and contain no control characters")
    text_limit = 6 if request.language in (1, 8, 9, 10) else 12
    for label, text in (("OT name", request.ot_name), ("nickname", request.nickname)):
        if text is not None and len(text.encode("utf-16-le")) // 2 > text_limit:
            raise ValueError(f"{label} exceeds the language-specific limit ({text_limit} UTF-16 units)")
    met_date = request.met_date if request.met_date is not None else date.today()
    if type(met_date) is not date or not 2000 <= met_date.year <= 2099:
        raise ValueError("met date must be a real date in 2000..2099")
    if request.encounter == "starter":
        if request.species not in STARTERS or request.form != 0:
            raise ValueError("starter preset supports National IDs 1,4,7,152,158,498 (form 0); use explicit custom encounter for others")
        location, scale, preset_moves = STARTERS[request.species]
        if request.met_location not in (None, location) or request.met_level not in (None, 5) or request.ball != 4:
            raise ValueError("starter preset fixes met level 5, Poké Ball and its documented location")
        met_level, level, flawless = 5, request.level if request.level is not None else 5, 3
        if level < met_level:
            raise ValueError("current level cannot be lower than met level")
        moves = request.moves if request.moves is not None else (preset_moves if level == 5 and preset_moves else encounter_moves(p, level))
    else:
        if request.met_location is None or request.met_level is None or request.level is None:
            raise ValueError("custom encounter requires level, met_level and met_location; it is not a verified encounter")
        location, met_level, level = request.met_location, request.met_level, request.level
        _int(location, 1, 65535, "met location")
        _int(met_level, 1, 100, "met level")
        if level < met_level:
            raise ValueError("current level cannot be lower than met level")
        scale, flawless = None, 0
        moves = request.moves if request.moves is not None else encounter_moves(p, level)
    _int(request.ball, 1, 37, "ball ID")
    _int(level, 1, 100, "level")
    seed = secrets.randbits(64) if request.seed is None else _int(request.seed, 0, (1 << 64) - 1, "seed")
    record = PA9()
    record.species_internal, record.form = p["internal_id"], request.form
    record.experience = experience_for_level(level, p["growth"])
    record.original_trainer_name, record.tid, record.sid = request.ot_name, request.tid, request.sid
    record.ot_gender, record.language, record.version = request.ot_gender, request.language, 52
    record.ot_friendship = p["friendship"]
    record.nickname = request.nickname if request.nickname is not None else p["names"][str(request.language)]
    record.is_nicknamed = request.nickname is not None
    record.met_year, record.met_month, record.met_day = met_date.year - 2000, met_date.month, met_date.day
    record.met_location, record.met_level, record.obedience_level = location, met_level, met_level
    record.ball = request.ball
    record = _pinga(record, p, seed, flawless=flawless, scale=scale)
    record = _moves_and_plus(record, p, level, tuple(moves))
    return recalculate_party(record)


def creation_summary(record: PA9, *, encounter: str) -> dict:
    p = personal_for_record(record)
    return {"species_national": p["species"], "species_internal": record.species_internal,
            "form": record.form, "bytes": record.size, "stats": calculate_stats(record),
            "checksum_valid": record.checksum_valid, "encounter_model": encounter,
            "legality": "not_checked", "game_load": "not_tested", "physical_trade": "not_tested",
            "source_commit": SOURCE_COMMIT}
