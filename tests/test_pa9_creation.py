"""Generated synthetic inputs only; no original save, PA9, ROM or keys."""
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import socket
import subprocess

import pytest

from zasim.pa9 import PA9, STORED_SIZE, PARTY_SIZE
from zasim.pa9_creation import (
    CreateRequest, STARTERS, STAT_NAMES, SOURCE_COMMIT, _data, create_pa9, creation_summary, experience_for_level,
    level_for_experience, personal_info, recalculate_party, species_catalog,
)
from zasim.pa9_editor import PA9EditorDocument

ROOT = Path(__file__).parent
REF = json.loads((ROOT / "pa9_creation_reference.json").read_text())
STARTER_REF = json.loads((ROOT / "pa9_starter_reference.json").read_text())
BASE = CreateRequest(152, "テスト", 12345, 54321, seed=1, met_date=date(2026, 10, 5))


@pytest.mark.parametrize("vector", REF["vectors"], ids=lambda v: f'{v["species"]}-{v["form"]}')
def test_stats_against_public_core_independent_vectors(vector):
    v = vector
    assert REF["source_commit"] == SOURCE_COMMIT
    p = personal_info(v["species"], v["form"])
    assert p["internal_id"] == v["internal_id"]
    m = PA9()
    m.species_internal, m.form, m.stat_nature = v["internal_id"], v["form"], v["nature"]
    m.experience = experience_for_level(v["level"], p["growth"])
    assert m.experience == v["exp"]
    for name, iv, ev in zip(STAT_NAMES, v["ivs"], v["evs"]):
        setattr(m, "iv_" + name, iv)
        setattr(m, "ev_" + name, ev)
    raw = bytearray(m.data)
    raw[0x126] = v["hyper"]
    m = PA9(raw, encrypted=False, validate=False)
    m.update_checksum()
    result = recalculate_party(m)
    assert [result.stat_hp_max, result.stat_atk, result.stat_def, result.stat_spa, result.stat_spd, result.stat_spe] == v["stats"]
    assert result.stat_level == v["level"] and result.checksum_valid
    assert result.stat_hp_current == result.stat_hp_max


@pytest.mark.parametrize("case", STARTER_REF["cases"], ids=lambda c: c["file"])
def test_starter_samples_match_independently_accepted_core_stats(case):
    _, species, seed = Path(case["file"]).stem.split("-")
    m = create_pa9(replace(BASE, species=int(species), seed=int(seed)))
    assert case["legality"] and case["stats_match"] and case["structural"]
    assert m.size == 344 and m.stat_level == 5
    assert [m.stat_hp_max, m.stat_atk, m.stat_def, m.stat_spa, m.stat_spd, m.stat_spe] == case["expected_stats"]
    assert sum(getattr(m, "iv_"+n) == 31 for n in STAT_NAMES) >= 3
    assert m.version == 52 and m.met_level == m.obedience_level == 5
    assert m.met_location == STARTERS[int(species)][0]
    assert m.ball == 4 and m.data[0xD4] == 255
    assert not m.is_egg and not m.is_alpha
    assert ((m.pid >> 16) ^ (m.pid & 65535) ^ m.tid ^ m.sid) >= 16
    assert all(getattr(m, f'move{i}_pp') == _data()['pp'][getattr(m, f'move{i}')] for i in range(1,5))
    # External sample acceptance is not a bundled general-purpose legality check.
    assert creation_summary(m, encounter="starter")["legality"] == "not_checked"
    assert PA9(m.to_bytes(encrypted=True), encrypted=True).data == m.data


@pytest.mark.parametrize("growth", range(6))
def test_every_growth_threshold_and_between_level(growth):
    for level in range(1, 101):
        exp = experience_for_level(level, growth)
        assert level_for_experience(exp, growth) == level
        if level < 100:
            assert level_for_experience(experience_for_level(level+1, growth)-1, growth) == level
    with pytest.raises(ValueError):
        level_for_experience(experience_for_level(100,growth)+1,growth)


@pytest.mark.parametrize("changes", [
    {"species": 0}, {"species": 133}, {"form": 1}, {"level": 0}, {"level": 101},
    {"tid": 65536}, {"sid": -1}, {"tid": True}, {"ot_name": ""}, {"ot_name": "Trainer"},
    {"ot_name": "ABC\n"}, {"ot_name": "a\0"}, {"ot_name": "\ud800"},
    {"nickname": "あいうえおかき"}, {"nickname": ""}, {"language": 6}, {"ot_gender": 2},
    {"seed": -1}, {"seed": 1 << 64}, {"met_date": date(2100,1,1)},
    {"met_location": 1}, {"met_level": 6}, {"ball": 1}, {"moves": (99999,)},
    {"moves": (33,33)}, {"moves": (0,)}, {"moves": (94,)}, {"encounter": "unknown"},
    {"encounter": "custom"}, {"encounter": "custom", "level": 5, "met_level": 6, "met_location": 1},
])
def test_invalid_creation_is_not_silently_repaired(changes):
    with pytest.raises((TypeError, ValueError)):
        create_pa9(replace(BASE, **changes))


def test_custom_explicit_metadata_internal_id_mapping_and_moves():
    # Gen9 national 978 (Tatsugiri) is not its internal species number.
    r = replace(BASE,species=978,encounter="custom",level=50,met_level=30,met_location=1)
    m = create_pa9(r)
    assert m.species_internal == personal_info(978)["internal_id"] != 978
    assert m.stat_level == 50 and m.met_level == 30 and m.met_location == 1
    assert m.move1 != 0 and m.stat_hp_max > 0
    assert creation_summary(m,encounter="custom")["game_load"] == "not_tested"
    assert len(species_catalog()) == 490
    with pytest.raises(ValueError): personal_info(6,1)  # Mega form
    with pytest.raises(ValueError): personal_info(999,255)


def test_no_save_no_executable_no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("generation must not execute/connect")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    m = create_pa9(BASE)
    assert m.stat_hp_max > 0 and m.size == PARTY_SIZE
    assert create_pa9(BASE).data == m.data
    assert create_pa9(replace(BASE,seed=2)).data != m.data


def test_new_document_dirty_auto_stats_reset_save_and_stored_conversion(tmp_path):
    doc = PA9EditorDocument.create(BASE)
    assert doc.dirty and doc.path is None and doc.auto_recalculate
    before = doc.record.data
    doc.apply({"stat_level": 50, "iv_hp":31, "ev_hp":252, "stat_nature":3})
    assert doc.record.stat_level == 50 and doc.record.stat_hp_max == 152
    assert not doc.derived_stats_may_be_stale
    doc.reset()
    assert doc.record.data == before and doc.dirty
    path = doc.save(tmp_path/"new.epa9")
    assert not doc.dirty
    loaded = PA9EditorDocument.load(path)
    assert not loaded.auto_recalculate and loaded.record.data == doc.record.data
    stored = PA9(doc.record.to_bytes(party=False),encrypted=False)
    assert stored.size == STORED_SIZE
    with pytest.raises(ValueError,match="explicit"):
        recalculate_party(stored)
    party = recalculate_party(stored,promote=True)
    assert party.size == PARTY_SIZE and party.data == doc.record.data
    doc2 = PA9EditorDocument(stored)
    doc2.recalculate(promote=True)
    assert doc2.dirty and doc2.record.stat_hp_max > 0
    assert doc2.record.data == party.data


def test_auto_failure_transaction_and_exp_level_conflict():
    doc = PA9EditorDocument.create(BASE)
    before = doc.record.data
    with pytest.raises(ValueError): doc.apply({"species_internal":0})
    assert doc.record.data == before
    with pytest.raises(ValueError,match="conflict"):
        doc.apply({"stat_level":50,"experience":100})
    assert doc.record.data == before
    with pytest.raises(ValueError,match="EV total"):
        doc.apply({"ev_hp":252,"ev_atk":252,"ev_def":252})
    assert doc.record.data == before
    doc.apply({"experience":experience_for_level(100,personal_info(152)["growth"])})
    assert doc.record.stat_level == 100


def test_recalculation_preserves_unrelated_bytes_and_heals():
    m = create_pa9(BASE)
    raw = bytearray(m.data)
    raw[0x140:0x148] = bytes(range(8))  # preserve hypothetical HOME tracker
    raw[0x90:0x94] = bytes([1,2,3,4])
    m = PA9(raw,encrypted=False,validate=False)
    m.stat_hp_current = 0
    m.update_checksum()
    result = recalculate_party(m,level=50)
    assert result.data[0x140:0x148] == bytes(range(8))
    assert result.data[0x90:0x94] == bytes(4)
    assert result.stat_hp_current == result.stat_hp_max > 0
    allowed = set(range(6,8))|set(range(0x10,0x14))|set(range(0x8A,0x8C))|set(range(0x90,0x94))|set(range(0x148,0x158))
    assert all(a == b or i in allowed for i,(a,b) in enumerate(zip(m.data,result.data)))


def test_cli_create_edit_promote_and_no_clobber(tmp_path,capsys):
    from zatrade import main
    output = tmp_path/"new.pa9"
    args = ["create-pa9","--species","152","--ot-name","テスト","--tid","12345","--sid","54321","--seed","1","--met-date","2026-10-05","--output",str(output)]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["stats"]["stat_level"] == 5 and report["legality"] == "not_checked"
    before = output.read_bytes()
    with pytest.raises(SystemExit): main(args)
    assert output.read_bytes() == before
    capsys.readouterr()
    result = tmp_path/"level50.pa9"
    assert main(["edit-pa9",str(output),"--output",str(result),"--level","50"]) == 0
    assert PA9(result.read_bytes()).stat_level == 50
    assert output.read_bytes() == before
    capsys.readouterr()
    stored = tmp_path/"stored.pa9"
    stored.write_bytes(PA9(before).to_bytes(party=False))
    promoted = tmp_path/"party.pa9"
    assert main(["edit-pa9",str(stored),"--output",str(promoted),"--party"]) == 0
    assert promoted.read_bytes() == before
    assert stored.stat().st_size == STORED_SIZE


@pytest.mark.parametrize("extra", [["--moves","not-a-number"],["--met-date","2026-02-30"],["--species","0"],["--level","200"]])
def test_cli_invalid_request_writes_nothing(extra,tmp_path,capsys):
    from zatrade import main
    target = tmp_path/"bad.pa9"
    with pytest.raises(SystemExit) as exc:
        main(["create-pa9","--species","152","--ot-name","テスト","--tid","1","--sid","2","--output",str(target),*extra])
    assert exc.value.code == 2 and not target.exists()
    capsys.readouterr()


def test_all_catalog_entries_create_calculated_custom_drafts():
    for p in species_catalog():
        m = create_pa9(replace(BASE, species=p["species"], form=p["form"],
                              encounter="custom", level=100, met_level=1, met_location=1))
        assert m.size == 344 and m.stat_level == 100 and m.stat_hp_max > 0
        assert m.move1 > 0 and m.checksum_valid


def test_empty_requested_moves_are_rejected():
    with pytest.raises(ValueError, match="1..4"):
        create_pa9(replace(BASE, moves=()))


def test_data_corruption_fails_closed(tmp_path, monkeypatch):
    import zasim.pa9_creation as creation
    data = tmp_path / "data"
    data.mkdir()
    (data / "za_creation.json").write_bytes(b"{}")
    (data / "za_creation_provenance.json").write_text(json.dumps({"data_sha256": "0" * 64}))
    creation._data.cache_clear()
    monkeypatch.setattr(creation, "files", lambda package: tmp_path)
    try:
        with pytest.raises(ValueError, match="integrity"):
            create_pa9(BASE)
    finally:
        creation._data.cache_clear()
