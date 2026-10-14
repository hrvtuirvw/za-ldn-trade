"""Headless PA9 editor tests using only synthetic records."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import threading

import pytest

from zasim.pa9 import PA9, PARTY_SIZE, STORED_SIZE, is_encrypted_pa9
import zasim.pa9_editor as editor
from zasim.pa9_editor import (
    EDITABLE_FIELDS,
    PA9EditorDocument,
    apply_pa9_changes,
    editor_values,
    encrypted_for_path,
    parse_assignments,
    snapshot_pa9_output_identity,
    validate_editor_record,
)


def _synthetic_record(*, party: bool = True) -> PA9:
    record = PA9() if party else PA9(bytes(STORED_SIZE))
    record.species_internal = 25
    record.nickname = "ZA-test"
    record.is_nicknamed = True
    record.nature = 3
    record.stat_nature = 3
    record.gender = 0
    record.original_trainer_name = "CODEX"
    record.met_month = 8
    record.met_day = 30
    if party:
        record.stat_level = 50
        record.stat_hp_current = 100
        record.stat_hp_max = 100
    record._data[0x33] = 0xA5  # deliberately unexposed synthetic byte
    record.update_checksum()
    return record


def test_all_editor_groups_apply_transactionally_and_preserve_unknown_bytes() -> None:
    original = _synthetic_record()
    before = original.data
    updated = apply_pa9_changes(
        original,
        {
            "nickname": "ミアレ😀",
            "species": "0x85",
            "experience": "123456",
            "ability": 42,
            "nature": 13,
            "is_alpha": "true",
            "move1": "0x55",
            "move1_pp": 10,
            "move1_pp_ups": 3,
            "iv_hp": 31,
            "ev_hp": 252,
            "stat_level": 60,
            "ot_name": "ZA親",
            "met_location": 900,
        },
    )

    assert original.data == before
    assert updated.species_internal == 0x85
    assert updated.nickname == "ミアレ😀"
    assert updated.move1 == 0x55
    assert updated.iv_hp == 31
    assert updated.ev_hp == 252
    assert updated.stat_level == 60
    assert updated.original_trainer_name == "ZA親"
    assert updated.data[0x33] == 0xA5
    assert updated.checksum_valid


@pytest.mark.parametrize("spec", EDITABLE_FIELDS, ids=lambda spec: spec.name)
def test_every_editable_field_enforces_its_boundaries(spec) -> None:
    record = _synthetic_record()
    if spec.kind == "text":
        updated = apply_pa9_changes(record, {spec.name: "A" * 12})
        assert getattr(updated, spec.name) == "A" * 12
        with pytest.raises(ValueError, match=spec.name):
            apply_pa9_changes(record, {spec.name: "A" * 13})
        return
    if spec.kind == "bool":
        assert getattr(apply_pa9_changes(record, {spec.name: True}), spec.name) is True
        assert getattr(apply_pa9_changes(record, {spec.name: False}), spec.name) is False
        with pytest.raises(ValueError, match=spec.name):
            apply_pa9_changes(record, {spec.name: 1})
        return

    assert spec.minimum is not None and spec.maximum is not None
    for value in (spec.minimum, spec.maximum):
        updated = apply_pa9_changes(record, {spec.name: value})
        assert getattr(updated, spec.name) == value
        assert updated.checksum_valid
    with pytest.raises(ValueError, match=spec.name):
        apply_pa9_changes(record, {spec.name: spec.minimum - 1})
    with pytest.raises(ValueError, match=spec.name):
        apply_pa9_changes(record, {spec.name: spec.maximum + 1})


def test_invalid_multi_field_change_does_not_modify_source() -> None:
    record = _synthetic_record()
    before = record.data
    with pytest.raises(ValueError, match="iv_hp"):
        apply_pa9_changes(record, {"species_internal": 133, "iv_hp": 32})
    assert record.data == before


def test_document_alias_collision_and_reverted_stale_change() -> None:
    document = PA9EditorDocument(_synthetic_record())
    with pytest.raises(ValueError, match="more than once"):
        document.apply({"species": 133, "species_internal": 25})
    document.apply({"stat_level": 60})
    assert document.derived_stats_may_be_stale
    document.apply({"stat_level": 50})
    assert not document.dirty
    assert not document.changed_fields
    assert not document.derived_stats_may_be_stale


def test_saved_stale_party_warning_survives_unrelated_edits_and_reset(
    tmp_path: Path,
) -> None:
    document = PA9EditorDocument(_synthetic_record())
    document.apply({"species_internal": 133})
    assert document.derived_stats_may_be_stale
    document.save(tmp_path / "stale.pa9")
    document.apply({"nickname": "renamed"})
    assert document.derived_stats_may_be_stale
    document.reset()
    assert document.derived_stats_may_be_stale


@pytest.mark.parametrize(
    ("assignments", "message"),
    [
        (["nickname=ABCDEFGHIJK😀"], "UTF-16"),
        (["nickname=a\0b"], "NUL"),
        (["nature=25"], "nature"),
        (["is_alpha=perhaps"], "true/false"),
        (["unknown=1"], "unknown"),
        (["iv_hp=1", "iv-hp=2"], "more than once"),
    ],
)
def test_assignment_validation(assignments: list[str], message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        parse_assignments(assignments)


def test_assignment_aliases_and_boolean_hex_values() -> None:
    values = parse_assignments(
        ["species=0x19", "item=42", "alpha=yes", "stat-alignment=0x0d"]
    )
    assert values == {
        "species_internal": 25,
        "held_item": 42,
        "is_alpha": True,
        "stat_nature": 13,
    }
    assert parse_assignments(["tid=00123"])["tid"] == 123


def test_stored_record_rejects_party_only_change() -> None:
    record = _synthetic_record(party=False)
    assert editor_values(record)["stat_level"] is None
    with pytest.raises(ValueError, match="party PA9"):
        apply_pa9_changes(record, {"stat_level": 50})


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("party", [False, True])
def test_document_load_save_matrix_preserves_size_and_format(
    tmp_path: Path, encrypted: bool, party: bool
) -> None:
    record = _synthetic_record(party=party)
    input_suffix = ".epa9" if encrypted else ".pa9"
    source = tmp_path / f"input{input_suffix}"
    source.write_bytes(record.to_bytes(encrypted=encrypted))

    document = PA9EditorDocument.load(source)
    assert document.input_encrypted is encrypted
    assert document.record.is_party is party
    document.apply({"nickname": "edited", "pid": "0x12345678"})
    assert document.dirty

    output = tmp_path / ("output.ea9" if not encrypted else "output.pa9")
    saved = document.save(output)
    assert saved == output
    assert output.stat().st_size == record.size
    assert is_encrypted_pa9(output.read_bytes()) is encrypted_for_path(output)
    reopened = PA9EditorDocument.load(output)
    assert reopened.record.nickname == "edited"
    assert reopened.record.pid == 0x12345678
    assert reopened.record.checksum_valid
    assert not document.dirty


def test_save_refuses_existing_target_without_force(tmp_path: Path) -> None:
    source = tmp_path / "source.pa9"
    source.write_bytes(_synthetic_record().to_bytes())
    target = tmp_path / "target.pa9"
    target.write_bytes(b"keep")
    document = PA9EditorDocument.load(source)
    with pytest.raises(FileExistsError, match="already exists"):
        document.save(target)
    assert target.read_bytes() == b"keep"
    document.save(target, overwrite=True)
    assert len(target.read_bytes()) == document.record.size


def test_in_place_save_rejects_external_change(tmp_path: Path) -> None:
    source = tmp_path / "source.pa9"
    source.write_bytes(_synthetic_record().to_bytes())
    document = PA9EditorDocument.load(source)
    document.apply({"nickname": "edited"})
    source.write_bytes(b"externally changed")

    with pytest.raises(FileExistsError, match="changed after approval"):
        document.save()
    assert source.read_bytes() == b"externally changed"
    assert document.dirty


def test_save_as_cannot_bypass_source_change_guard(tmp_path: Path) -> None:
    source = tmp_path / "source.pa9"
    source.write_bytes(_synthetic_record().to_bytes())
    document = PA9EditorDocument.load(source)
    document.apply({"nickname": "edited"})
    source.write_bytes(b"externally changed")

    with pytest.raises(ValueError, match="--in-place"):
        document.save(source, overwrite=True)
    assert source.read_bytes() == b"externally changed"
    assert document.dirty


def test_save_as_rejects_hardlink_alias_of_loaded_source(tmp_path: Path) -> None:
    source = tmp_path / "source.pa9"
    alias = tmp_path / "alias.pa9"
    original = _synthetic_record().to_bytes()
    source.write_bytes(original)
    try:
        os.link(source, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    document = PA9EditorDocument.load(source)
    document.apply({"nickname": "edited"})

    with pytest.raises(ValueError, match="in-place"):
        document.save(alias, overwrite=True)
    assert source.read_bytes() == original
    assert alias.read_bytes() == original


def test_save_as_rejects_target_changed_after_identity_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source.pa9"
    source.write_bytes(_synthetic_record().to_bytes())
    target = tmp_path / "target.pa9"
    target.write_bytes(b"version shown for approval")
    approved = snapshot_pa9_output_identity(target, overwrite=True)
    assert approved is not None
    target.write_bytes(b"changed while confirmation was open")
    document = PA9EditorDocument.load(source)

    with pytest.raises(FileExistsError, match="changed after approval"):
        document.save(
            target, overwrite=True, expected_identity=approved
        )
    assert target.read_bytes() == b"changed while confirmation was open"


def test_save_as_rejects_target_appearing_after_absent_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source.pa9"
    source.write_bytes(_synthetic_record().to_bytes())
    target = tmp_path / "target.pa9"
    approved = snapshot_pa9_output_identity(target, overwrite=True)
    assert approved is None
    target.write_bytes(b"new competing output")
    document = PA9EditorDocument.load(source)

    with pytest.raises(FileExistsError, match="changed after approval"):
        document.save(target, overwrite=True, expected_identity=approved)
    assert target.read_bytes() == b"new competing output"


def test_atomic_replace_failure_keeps_target_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pa9"
    source.write_bytes(_synthetic_record().to_bytes())
    target = tmp_path / "target.pa9"
    target.write_bytes(b"original target")
    document = PA9EditorDocument.load(source)
    document.apply({"nickname": "edited"})

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(editor.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic"):
        document.save(target, overwrite=True)
    assert target.read_bytes() == b"original target"
    assert not list(tmp_path.glob(".zatrade-pa9-write-*.tmp"))


def test_new_output_race_never_clobbers_competing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pa9"
    source.write_bytes(_synthetic_record().to_bytes())
    target = tmp_path / "new.pa9"
    document = PA9EditorDocument.load(source)

    def racing_publish(_source: object, destination: object) -> None:
        Path(destination).write_bytes(b"created by another process")
        raise FileExistsError("synthetic race")

    method = "rename" if os.name == "nt" else "link"
    monkeypatch.setattr(editor.os, method, racing_publish)
    with pytest.raises(FileExistsError, match="appeared after approval"):
        document.save(target)
    assert target.read_bytes() == b"created by another process"
    assert not list(tmp_path.glob(".zatrade-pa9-write-*.tmp"))


def test_two_editor_saves_to_same_target_are_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "shared.pa9"
    target.write_bytes(_synthetic_record().to_bytes())
    first = PA9EditorDocument.load(target)
    second = PA9EditorDocument.load(target)
    first.apply({"nickname": "FIRST"})
    second.apply({"nickname": "SECOND"})

    real_replace = editor.os.replace
    entered = threading.Event()
    release = threading.Event()

    def delayed_replace(source: object, destination: object) -> None:
        if Path(destination) == target and threading.current_thread().name == "first-save":
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release the first save")
        real_replace(source, destination)

    monkeypatch.setattr(editor.os, "replace", delayed_replace)
    failures: list[BaseException] = []

    def save_first() -> None:
        try:
            first.save()
        except BaseException as exc:  # captured so pytest gets the real failure
            failures.append(exc)

    worker = threading.Thread(target=save_first, name="first-save")
    worker.start()
    assert entered.wait(5), "first writer did not reach atomic publication"
    try:
        with pytest.raises(FileExistsError, match="already in progress"):
            second.save()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert failures == []
    assert PA9EditorDocument.load(target).record.nickname == "FIRST"
    assert not first.dirty
    assert second.dirty


def test_post_publish_race_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "shared.pa9"
    target.write_bytes(_synthetic_record().to_bytes())
    document = PA9EditorDocument.load(target)
    document.apply({"nickname": "edited"})
    real_replace = editor.os.replace

    def replace_then_compete(source: object, destination: object) -> None:
        real_replace(source, destination)
        Path(destination).write_bytes(b"competing writer")

    monkeypatch.setattr(editor.os, "replace", replace_then_compete)
    with pytest.raises(RuntimeError, match="cannot be confirmed"):
        document.save()
    assert target.read_bytes() == b"competing writer"
    assert document.dirty


def test_temp_cleanup_error_does_not_mask_replace_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pa9"
    source.write_bytes(_synthetic_record().to_bytes())
    target = tmp_path / "target.pa9"
    target.write_bytes(b"original")
    document = PA9EditorDocument.load(source)
    original_unlink = editor.Path.unlink

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("primary replace failure")

    def fail_write_temp_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path.name.startswith(".zatrade-pa9-write-"):
            raise PermissionError("secondary cleanup failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(editor.os, "replace", fail_replace)
    monkeypatch.setattr(editor.Path, "unlink", fail_write_temp_unlink)
    with pytest.raises(RuntimeError, match="primary replace failure") as raised:
        document.save(target, overwrite=True)
    assert "temporary PA9 data" in str(raised.value)
    monkeypatch.setattr(editor.Path, "unlink", original_unlink)
    for leftover in tmp_path.glob(".zatrade-pa9-write-*.tmp"):
        leftover.unlink()
    assert target.read_bytes() == b"original"


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only attribute behavior")
def test_windows_readonly_source_is_rejected_without_temp_leak(tmp_path: Path) -> None:
    source = tmp_path / "readonly.pa9"
    source.write_bytes(_synthetic_record().to_bytes())
    os.chmod(source, stat.S_IREAD)
    try:
        document = PA9EditorDocument.load(source)
        document.apply({"nickname": "edited"})
        with pytest.raises(PermissionError, match="read-only"):
            document.save()
        assert not list(tmp_path.glob(".zatrade-pa9-write-*.tmp"))
        assert PA9EditorDocument.load(source).record.nickname == "ZA-test"
    finally:
        os.chmod(source, stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_overwrite_preserves_existing_permission_mode(tmp_path: Path) -> None:
    source = tmp_path / "source.pa9"
    source.write_bytes(_synthetic_record().to_bytes())
    source.chmod(0o640)
    document = PA9EditorDocument.load(source)
    document.apply({"nickname": "edited"})
    document.save()
    assert stat.S_IMODE(source.stat().st_mode) == 0o640


@pytest.mark.skipif(os.name == "nt", reason="POSIX filename limit scenario")
def test_long_valid_output_name_does_not_break_temp_creation(tmp_path: Path) -> None:
    source = tmp_path / "source.pa9"
    source.write_bytes(_synthetic_record().to_bytes())
    target = tmp_path / ("x" * 240 + ".pa9")
    PA9EditorDocument.load(source).save(target)
    assert target.stat().st_size == PARTY_SIZE


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is POSIX-only")
def test_directory_fsync_io_error_reports_ambiguous_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pa9"
    source.write_bytes(_synthetic_record().to_bytes())
    document = PA9EditorDocument.load(source)
    document.apply({"nickname": "edited"})
    real_fsync = editor.os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "synthetic directory I/O failure")
        real_fsync(descriptor)

    monkeypatch.setattr(editor.os, "fsync", fail_directory_fsync)
    with pytest.raises(OSError, match="was published"):
        document.save()
    assert PA9(source.read_bytes(), encrypted=False).nickname == "edited"
    assert document.dirty


def test_load_rejects_bad_size_before_identity_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "huge.pa9"
    source.write_bytes(bytes(1024 * 1024))

    def identity_must_not_run(_path: object) -> object:
        raise AssertionError("identity hashing should not run")

    monkeypatch.setattr(editor, "file_identity", identity_must_not_run)
    with pytest.raises(ValueError, match="exactly"):
        PA9EditorDocument.load(source)


def test_load_rejects_unpaired_utf16_in_editable_text(tmp_path: Path) -> None:
    record = _synthetic_record()
    record._data[0x58:0x5C] = b"\x00\xD8\x00\x00"
    record.update_checksum()
    source = tmp_path / "bad-name.pa9"
    source.write_bytes(record.to_bytes())
    with pytest.raises(ValueError, match="editable Unicode"):
        PA9EditorDocument.load(source)


def test_bad_checksum_uses_explicit_suffix_as_encryption_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _synthetic_record()
    damaged = bytearray(record.to_bytes(encrypted=True))
    damaged[0x20] ^= 1
    source = tmp_path / "damaged.epa9"
    source.write_bytes(damaged)
    monkeypatch.setattr(editor, "is_encrypted_pa9", lambda _raw: False)

    document = PA9EditorDocument.load(source)
    assert document.input_encrypted is True
    assert document.record.species_internal == record.species_internal
    assert not document.record.checksum_valid


def test_bad_checksum_unknown_suffix_is_rejected_as_ambiguous(tmp_path: Path) -> None:
    record = _synthetic_record()
    damaged = bytearray(record.to_bytes())
    damaged[0x20] ^= 1
    source = tmp_path / "damaged.bin"
    source.write_bytes(damaged)

    with pytest.raises(ValueError, match="encryption cannot be determined"):
        PA9EditorDocument.load(source)


def test_structural_diagnostics_do_not_claim_legality() -> None:
    record = _synthetic_record()
    record.ev_hp = 252
    record.ev_atk = 252
    record.ev_def = 252
    record.checksum = 0
    issues = validate_editor_record(record)
    assert any(issue.field == "checksum" for issue in issues)
    assert any("EV total" in issue.message for issue in issues)
    assert editor.pa9_editor_summary(record)["legality"] == "not_checked"


def test_structural_diagnostics_report_individual_ev_and_hp_mismatch() -> None:
    record = _synthetic_record()
    record.ev_hp = 253
    record.stat_hp_current = 101
    record.stat_hp_max = 100
    issues = validate_editor_record(record)
    assert any(issue.field == "ev_hp" and "at most 252" in issue.message for issue in issues)
    assert any("current HP" in issue.message for issue in issues)


def test_output_suffix_is_explicit() -> None:
    assert encrypted_for_path("x.pa9") is False
    assert encrypted_for_path("x.epa9") is True
    assert encrypted_for_path("x.ea9") is True
    with pytest.raises(ValueError, match="must use"):
        encrypted_for_path("x.bin")
