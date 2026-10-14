"""Headless PA9 editor shared by the Windows/Linux GUI and CLI.

The editor changes only documented PA9 fields, preserves every unexposed byte,
updates the additive checksum, and saves through a same-directory atomic
replacement.  Its checks are structural; it is deliberately not a Pokémon
legality engine. Explicit/new-document calculation uses public ZA personal
and growth data; the raw field editor remains byte-preserving by default.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from typing import Iterable, Mapping

from .pa9 import PA9, PARTY_SIZE, STORED_SIZE, is_encrypted_pa9
from .file_save import (
    FileIdentity, _absolute_path, _metadata_identity, file_identity,
    exclusive_save_lock, publish_approved, _sync_output_directory, _cleanup_owned_temporary,
    open_regular_binary,
)


SUPPORTED_PA9_SUFFIXES = {".pa9": False, ".ea9": True, ".epa9": True}
MAX_REPLACE_TARGET_BYTES = 1024 * 1024
_UNSET_OUTPUT_IDENTITY = object()


NATURE_NAMES_JA = (
    "がんばりや",
    "さみしがり",
    "ゆうかん",
    "いじっぱり",
    "やんちゃ",
    "ずぶとい",
    "すなお",
    "のんき",
    "わんぱく",
    "のうてんき",
    "おくびょう",
    "せっかち",
    "まじめ",
    "ようき",
    "むじゃき",
    "ひかえめ",
    "おっとり",
    "れいせい",
    "てれや",
    "うっかりや",
    "おだやか",
    "おとなしい",
    "なまいき",
    "しんちょう",
    "きまぐれ",
)

GENDER_NAMES_JA = ("♂ オス", "♀ メス", "― 性別不明")


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One editable PA9 field and its UI/CLI validation metadata."""

    name: str
    label: str
    group: str
    kind: str = "int"
    minimum: int | None = None
    maximum: int | None = None
    hexadecimal: bool = False
    party_only: bool = False
    max_utf16_units: int | None = None


def _integer(
    name: str,
    label: str,
    group: str,
    maximum: int,
    *,
    minimum: int = 0,
    hexadecimal: bool = False,
    party_only: bool = False,
) -> FieldSpec:
    return FieldSpec(
        name,
        label,
        group,
        minimum=minimum,
        maximum=maximum,
        hexadecimal=hexadecimal,
        party_only=party_only,
    )


def _boolean_field(name: str, label: str, group: str) -> FieldSpec:
    return FieldSpec(name, label, group, kind="bool")


EDITABLE_FIELDS = (
    FieldSpec(
        "nickname", "ニックネーム", "main", kind="text", max_utf16_units=12
    ),
    _integer("species_internal", "Species内部ID", "main", 0xFFFF),
    _integer("held_item", "持ち物ID", "main", 0xFFFF),
    _integer("experience", "経験値", "main", 0xFFFFFFFF),
    _integer("ability", "特性ID", "main", 0xFFFF),
    _integer("ability_number", "特性スロット", "main", 7),
    _integer("nature", "性格", "main", 24),
    _integer("stat_nature", "補正性格 (Mint)", "main", 24),
    _integer("gender", "性別", "main", 2),
    _integer("form", "フォルム", "main", 0xFF),
    _boolean_field("is_alpha", "オヤブン", "main"),
    _boolean_field("fateful_encounter", "運命的な出会い", "main"),
    _boolean_field("is_egg", "タマゴ", "main"),
    _boolean_field("is_nicknamed", "ニックネーム設定済み", "main"),
    _integer("tid", "TID", "main", 0xFFFF),
    _integer("sid", "SID", "main", 0xFFFF),
    _integer("pid", "PID", "main", 0xFFFFFFFF, hexadecimal=True),
    _integer("ec", "EC", "main", 0xFFFFFFFF, hexadecimal=True),
    *tuple(
        spec
        for slot in range(1, 5)
        for spec in (
            _integer(f"move{slot}", f"技{slot} ID", "moves", 0xFFFF),
            _integer(f"move{slot}_pp", f"技{slot} PP", "moves", 0xFF),
            _integer(f"move{slot}_pp_ups", f"技{slot} PP Up", "moves", 3),
            _integer(
                f"relearn_move{slot}", f"思い出し技{slot} ID", "moves", 0xFFFF
            ),
        )
    ),
    _integer("iv_hp", "HP IV", "stats", 31),
    _integer("iv_atk", "Attack IV", "stats", 31),
    _integer("iv_def", "Defense IV", "stats", 31),
    _integer("iv_spa", "Sp. Attack IV", "stats", 31),
    _integer("iv_spd", "Sp. Defense IV", "stats", 31),
    _integer("iv_spe", "Speed IV", "stats", 31),
    _integer("ev_hp", "HP EV", "stats", 252),
    _integer("ev_atk", "Attack EV", "stats", 252),
    _integer("ev_def", "Defense EV", "stats", 252),
    _integer("ev_spa", "Sp. Attack EV", "stats", 252),
    _integer("ev_spd", "Sp. Defense EV", "stats", 252),
    _integer("ev_spe", "Speed EV", "stats", 252),
    _integer("stat_level", "Party Level", "stats", 100, minimum=1, party_only=True),
    _integer("stat_hp_current", "Current HP", "stats", 0xFFFF),
    _integer("stat_hp_max", "Maximum HP", "stats", 0xFFFF, party_only=True),
    _integer("stat_atk", "Party Attack", "stats", 0xFFFF, party_only=True),
    _integer("stat_def", "Party Defense", "stats", 0xFFFF, party_only=True),
    _integer("stat_spa", "Party Sp. Attack", "stats", 0xFFFF, party_only=True),
    _integer("stat_spd", "Party Sp. Defense", "stats", 0xFFFF, party_only=True),
    _integer("stat_spe", "Party Speed", "stats", 0xFFFF, party_only=True),
    FieldSpec(
        "original_trainer_name",
        "親名",
        "ot",
        kind="text",
        max_utf16_units=12,
    ),
    _integer("language", "言語ID", "ot", 0xFF),
    _integer("version", "出身version ID", "ot", 0xFF),
    _integer("ot_friendship", "なつき度", "ot", 0xFF),
    _integer("ot_gender", "親の性別", "ot", 1),
    _integer("met_year", "出会い年 (2000+)", "ot", 0xFF),
    _integer("met_month", "出会い月", "ot", 12),
    _integer("met_day", "出会い日", "ot", 31),
    _integer("met_level", "出会いLv", "ot", 100),
    _integer("obedience_level", "従うLv", "ot", 100),
    _integer("met_location", "出会い場所ID", "ot", 0xFFFF),
    _integer("egg_location", "タマゴ場所ID", "ot", 0xFFFF),
    _integer("ball", "ボールID", "ot", 0xFF),
)

FIELD_SPECS = {spec.name: spec for spec in EDITABLE_FIELDS}
FIELD_ALIASES = {
    "species": "species_internal",
    "item": "held_item",
    "exp": "experience",
    "alpha": "is_alpha",
    "ot_name": "original_trainer_name",
    "stat_alignment": "stat_nature",
}

DERIVED_STAT_INPUTS = {
    "species_internal",
    "experience",
    "nature",
    "stat_nature",
    "form",
    "iv_hp",
    "iv_atk",
    "iv_def",
    "iv_spa",
    "iv_spd",
    "iv_spe",
    "ev_hp",
    "ev_atk",
    "ev_def",
    "ev_spa",
    "ev_spd",
    "ev_spe",
    "stat_level",
}


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One structural editor diagnostic."""

    severity: str
    field: str | None
    message: str


def _exclusive_save_lock(target: "_OutputTarget"):
    return exclusive_save_lock(target.path)


def encrypted_for_path(path: str | Path) -> bool:
    """Return output encryption selected by a supported PA9 suffix."""

    target = Path(path)
    try:
        return SUPPORTED_PA9_SUFFIXES[target.suffix.lower()]
    except KeyError as exc:
        raise ValueError("PA9 output must use .pa9, .epa9, or .ea9") from exc


def _decode_best_effort(
    raw: bytes, *, suffix: str | None = None
) -> tuple[PA9, bool]:
    heuristic = is_encrypted_pa9(raw)
    suffix_hint = SUPPORTED_PA9_SUFFIXES.get((suffix or "").lower())
    candidates: list[tuple[PA9, bool]] = []
    for encrypted in (heuristic, not heuristic):
        record = PA9(raw, encrypted=encrypted, validate=False)
        if record.sanity == 0 and record.checksum_valid:
            candidates.append((record, encrypted))
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        if suffix_hint is not None:
            return next(
                candidate for candidate in candidates if candidate[1] == suffix_hint
            )
        return next(
            candidate for candidate in candidates if candidate[1] == heuristic
        )
    if suffix_hint is None:
        raise ValueError(
            "PA9 checksum is invalid and encryption cannot be determined; "
            "use a .pa9 suffix for decrypted data or .epa9/.ea9 for encrypted data"
        )
    return PA9(raw, encrypted=suffix_hint, validate=False), suffix_hint


def _read_regular_pa9(path: str | Path) -> tuple[Path, bytes, FileIdentity]:
    target = _absolute_path(path)
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        raise FileNotFoundError(f"PA9 file does not exist: {target}") from None
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"PA9 path is not a regular file: {target}")
    if metadata.st_size not in {STORED_SIZE, PARTY_SIZE}:
        raise ValueError(
            f"PA9 file must be exactly 0x{STORED_SIZE:X} or 0x{PARTY_SIZE:X} "
            f"bytes; got {metadata.st_size}"
        )
    before = file_identity(target)
    if before is None:
        raise FileNotFoundError(f"PA9 file does not exist: {target}")
    with open_regular_binary(target, max_size=PARTY_SIZE) as stream:
        opened = os.fstat(stream.fileno())
        if _metadata_identity(opened) != (
            before.device,
            before.inode,
            before.size,
            before.modified_ns,
            before.changed_ns,
            before.mode,
        ):
            raise RuntimeError(f"PA9 file changed while opening: {target}")
        raw = stream.read(PARTY_SIZE + 1)
    if file_identity(target) != before:
        raise RuntimeError(f"PA9 file changed while reading: {target}")
    if len(raw) != before.size or hashlib.sha256(raw).hexdigest() != before.sha256:
        raise RuntimeError(f"PA9 file changed while reading: {target}")
    return target, raw, before


def normalize_field_name(name: str) -> str:
    """Normalize a CLI field name and resolve supported aliases."""

    if not isinstance(name, str):
        raise TypeError("field name must be a string")
    normalized = name.strip().lower().replace("-", "_")
    normalized = FIELD_ALIASES.get(normalized, normalized)
    if normalized not in FIELD_SPECS:
        available = ", ".join(FIELD_SPECS)
        raise ValueError(f"unknown PA9 editor field {name!r}; available: {available}")
    return normalized


def _validate_editor_text(value: object, spec: FieldSpec) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{spec.name} must be a string")
    if "\0" in value:
        raise ValueError(f"{spec.name} must not contain NUL")
    try:
        encoded = value.encode("utf-16-le")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{spec.name} contains an unpaired Unicode surrogate") from exc
    units = len(encoded) // 2
    if spec.max_utf16_units is not None and units > spec.max_utf16_units:
        raise ValueError(
            f"{spec.name} is {units} UTF-16 code units; maximum is "
            f"{spec.max_utf16_units}"
        )
    return value


def parse_editor_value(spec: FieldSpec, value: object) -> object:
    """Parse and range-check one GUI/CLI value according to *spec*."""

    if spec.kind == "text":
        return _validate_editor_text(value, spec)
    if spec.kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        raise ValueError(f"{spec.name} must be true/false, yes/no, on/off, or 1/0")
    if isinstance(value, bool):
        raise TypeError(f"{spec.name} must be an integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        unsigned = text[1:] if text[:1] in {"+", "-"} else text
        base = 0 if unsigned.lower().startswith(("0x", "0o", "0b")) else 10
        try:
            parsed = int(text, base)
        except ValueError as exc:
            raise ValueError(f"{spec.name} must be an integer") from exc
    else:
        raise TypeError(f"{spec.name} must be an integer")
    if spec.minimum is not None and parsed < spec.minimum:
        raise ValueError(f"{spec.name} must be at least {spec.minimum}; got {parsed}")
    if spec.maximum is not None and parsed > spec.maximum:
        raise ValueError(f"{spec.name} must be at most {spec.maximum}; got {parsed}")
    return parsed


def parse_assignments(assignments: Iterable[str]) -> dict[str, object]:
    """Parse repeatable ``FIELD=VALUE`` CLI assignments."""

    changes: dict[str, object] = {}
    for assignment in assignments:
        if not isinstance(assignment, str) or "=" not in assignment:
            raise ValueError("each --set value must use FIELD=VALUE")
        raw_name, raw_value = assignment.split("=", 1)
        name = normalize_field_name(raw_name)
        if name in changes:
            raise ValueError(f"field was specified more than once: {name}")
        changes[name] = parse_editor_value(FIELD_SPECS[name], raw_value)
    if not changes:
        raise ValueError("at least one --set FIELD=VALUE change is required")
    return changes


def editor_values(record: PA9) -> dict[str, object | None]:
    """Return all editable values, using ``None`` for absent party fields."""

    if not isinstance(record, PA9):
        raise TypeError("record must be a PA9")
    values: dict[str, object | None] = {}
    for spec in EDITABLE_FIELDS:
        values[spec.name] = None if spec.party_only and not record.is_party else getattr(
            record, spec.name
        )
    return values


def apply_pa9_changes(record: PA9, changes: Mapping[str, object]) -> PA9:
    """Apply validated changes transactionally and return a new PA9 object."""

    if not isinstance(record, PA9):
        raise TypeError("record must be a PA9")
    if not isinstance(changes, Mapping):
        raise TypeError("changes must be a mapping")
    parsed: dict[str, object] = {}
    for raw_name, value in changes.items():
        name = normalize_field_name(raw_name)
        if name in parsed:
            raise ValueError(f"field was specified more than once: {name}")
        spec = FIELD_SPECS[name]
        if spec.party_only and not record.is_party:
            raise ValueError(f"{name} can only be edited in a 0x{PARTY_SIZE:X} party PA9")
        parsed[name] = parse_editor_value(spec, value)

    updated = PA9(record.data, encrypted=False, validate=False)
    for name, value in parsed.items():
        if getattr(updated, name) != value:
            setattr(updated, name, value)
    updated.update_checksum()
    updated.validate()
    return updated


def validate_editor_record(record: PA9) -> tuple[ValidationIssue, ...]:
    """Return structural diagnostics without claiming full game legality."""

    if not isinstance(record, PA9):
        raise TypeError("record must be a PA9")
    issues: list[ValidationIssue] = []
    if record.sanity != 0:
        issues.append(
            ValidationIssue("error", "sanity", f"sanity is 0x{record.sanity:04X}, expected 0")
        )
    if not record.checksum_valid:
        issues.append(
            ValidationIssue("warning", "checksum", "checksum is invalid; saving will repair it")
        )
    if record.species_internal == 0:
        issues.append(ValidationIssue("warning", "species_internal", "empty species ID"))
    for spec in EDITABLE_FIELDS:
        if spec.party_only and not record.is_party:
            continue
        value = getattr(record, spec.name)
        try:
            parse_editor_value(spec, value)
        except (TypeError, ValueError) as exc:
            severity = "error" if spec.kind == "text" else "warning"
            issues.append(ValidationIssue(severity, spec.name, str(exc)))
    ev_names = ("ev_hp", "ev_atk", "ev_def", "ev_spa", "ev_spd", "ev_spe")
    ev_values = tuple(getattr(record, name) for name in ev_names)
    ev_total = sum(ev_values)
    if ev_total > 510:
        issues.append(
            ValidationIssue("warning", None, f"EV total is {ev_total}; normal maximum is 510")
        )
    if record.is_party:
        if record.stat_hp_max == 0:
            issues.append(
                ValidationIssue("warning", "stat_hp_max", "party maximum HP is zero")
            )
        elif record.stat_hp_current > record.stat_hp_max:
            issues.append(
                ValidationIssue(
                    "warning", "stat_hp_current", "current HP exceeds maximum HP"
                )
            )
    return tuple(issues)


def pa9_editor_summary(record: PA9) -> dict[str, object]:
    """Create a concise, JSON-friendly editor summary."""

    issues = validate_editor_record(record)
    return {
        "record_size": record.size,
        "format": "party" if record.is_party else "stored",
        "species_internal": record.species_internal,
        "nickname": record.nickname,
        "checksum_valid": record.checksum_valid,
        "sanity": record.sanity,
        "structural_errors": sum(issue.severity == "error" for issue in issues),
        "structural_warnings": sum(issue.severity == "warning" for issue in issues),
        "legality": "not_checked",
    }


@dataclass(frozen=True, slots=True)
class _OutputTarget:
    path: Path
    identity: FileIdentity | None


def _preflight_output(path: str | Path, *, overwrite: bool) -> _OutputTarget:
    target = _absolute_path(path)
    encrypted_for_path(target)
    if not target.parent.is_dir():
        raise ValueError(f"output directory does not exist: {target.parent}")
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None and metadata.st_size > MAX_REPLACE_TARGET_BYTES:
        raise ValueError(
            f"refusing to replace unexpectedly large PA9 output "
            f"({metadata.st_size} bytes): {target}"
        )
    identity = file_identity(target)
    if identity is not None and not overwrite:
        raise FileExistsError(f"PA9 output already exists: {target}")
    if (
        os.name == "nt"
        and identity is not None
        and not stat.S_IMODE(identity.mode) & stat.S_IWRITE
    ):
        raise PermissionError(
            f"PA9 output is read-only; clear the read-only attribute first: {target}"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".zatrade-pa9-preflight-", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    Path(temporary_name).unlink()
    return _OutputTarget(target, identity)


def snapshot_pa9_output_identity(
    path: str | Path, *, overwrite: bool
) -> FileIdentity | None:
    """Validate an output and snapshot exactly what an overwrite approves.

    Pass the result back to :meth:`PA9EditorDocument.save` as
    ``expected_identity``.  This keeps a confirmation dialog or earlier CLI
    decision from silently approving a file that changes before publication.
    """

    return _preflight_output(path, overwrite=overwrite).identity


def _atomic_write(
    record: PA9,
    target: _OutputTarget,
    *,
    acquire_lock: bool = True,
) -> tuple[PA9, FileIdentity]:
    updated = apply_pa9_changes(record, {})
    payload = updated.to_bytes(
        encrypted=encrypted_for_path(target.path),
        party=updated.is_party,
        update_checksum=True,
    )
    payload_digest = hashlib.sha256(payload).hexdigest()
    identity: FileIdentity | None = None
    lock_context = _exclusive_save_lock(target) if acquire_lock else nullcontext()
    with lock_context:
        current = file_identity(target.path)
        if target.identity is None:
            if current is not None:
                raise FileExistsError(
                    "PA9 output appeared after approval; refusing to overwrite: "
                    f"{target.path}"
                )
        elif current != target.identity:
            raise FileExistsError(
                f"PA9 output changed after approval; refusing to overwrite: {target.path}"
            )
        if (
            os.name == "nt"
            and target.identity is not None
            and not stat.S_IMODE(target.identity.mode) & stat.S_IWRITE
        ):
            raise PermissionError(
                "PA9 output is read-only; clear the read-only attribute first: "
                f"{target.path}"
            )

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".zatrade-pa9-write-", suffix=".tmp", dir=target.path.parent
        )
        temporary = Path(temporary_name)
        opened = False
        try:
            with os.fdopen(descriptor, "wb") as stream:
                opened = True
                if target.identity is not None and hasattr(os, "fchmod"):
                    os.fchmod(stream.fileno(), stat.S_IMODE(target.identity.mode))
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if target.identity is not None and not hasattr(os, "fchmod"):
                os.chmod(temporary, stat.S_IMODE(target.identity.mode))

            # Repeat the identity check immediately before publication.  The
            # cross-process lock serializes ZA-Trade writers; this second check
            # also catches ordinary non-cooperating edits during temp creation.
            publish_approved(
                temporary, target.path, target.identity, read_identity=file_identity
            )

            identity = file_identity(target.path)
            if (
                identity is None
                or identity.size != len(payload)
                or identity.sha256 != payload_digest
            ):
                raise RuntimeError(
                    "PA9 output changed during publication; save success cannot be "
                    f"confirmed: {target.path}"
                )
            _sync_output_directory(target.path.parent)
        except BaseException as operation_error:
            if not opened:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            cleanup_error = _cleanup_owned_temporary(temporary)
            if cleanup_error is not None and isinstance(operation_error, Exception):
                raise RuntimeError(
                    f"{operation_error}; additionally could not remove temporary "
                    f"PA9 data {temporary}: {cleanup_error}"
                ) from operation_error
            raise
        else:
            cleanup_error = _cleanup_owned_temporary(temporary)
            if cleanup_error is not None:
                raise RuntimeError(
                    "PA9 output was published, but temporary PA9 data could not "
                    f"be removed ({temporary}): {cleanup_error}"
                ) from cleanup_error
            # POSIX no-clobber uses a hard link. Removing our temporary link
            # changes the shared inode's ctime; record identity only afterwards
            # so this document can perform its next in-place save.
            identity = file_identity(target.path)
            if (identity is None or identity.size != len(payload)
                    or identity.sha256 != payload_digest):
                raise RuntimeError(
                    f"PA9 output changed during publication: {target.path}"
                )
            _sync_output_directory(target.path.parent)
    if identity is None:  # pragma: no cover - guarded by the verification above
        raise RuntimeError(f"PA9 output vanished after saving: {target.path}")
    return updated, identity


@dataclass(slots=True)
class PA9EditorDocument:
    """Mutable editor document with reset, dirty tracking, and atomic saves."""

    record: PA9
    path: Path | None = None
    input_encrypted: bool = False
    source_identity: FileIdentity | None = None
    _baseline: bytes = field(default=b"", repr=False)
    changed_fields: set[str] = field(default_factory=set)
    derived_stats_may_be_stale: bool = False
    _baseline_derived_stats_may_be_stale: bool = field(default=False, repr=False)
    auto_recalculate: bool = False
    _new_unsaved: bool = field(default=False, repr=False)
    _calculated_signature: tuple | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.record, PA9):
            raise TypeError("record must be a PA9")
        if not self._baseline:
            self._baseline = self.record.data
        if self.derived_stats_may_be_stale:
            self._baseline_derived_stats_may_be_stale = True

    @classmethod
    def load(cls, path: str | Path) -> "PA9EditorDocument":
        """Load an encrypted or decrypted stored/party PA9 regular file."""

        source, raw, identity = _read_regular_pa9(path)
        record, encrypted = _decode_best_effort(raw, suffix=source.suffix)
        for name in ("nickname", "original_trainer_name"):
            try:
                _validate_editor_text(getattr(record, name), FIELD_SPECS[name])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"PA9 {name} is not valid editable Unicode: {exc}") from exc
        return cls(
            record,
            path=source,
            input_encrypted=encrypted,
            source_identity=identity,
            _baseline=record.data,
        )

    @classmethod
    def create(cls, request) -> "PA9EditorDocument":
        """Create an unsaved party document, with auto calculation enabled."""
        from .pa9_creation import create_pa9
        document = cls(create_pa9(request), auto_recalculate=True, _new_unsaved=True)
        document._calculated_signature = document._stat_signature()
        return document

    def _stat_signature(self) -> tuple:
        return tuple(getattr(self.record, name) for name in sorted(DERIVED_STAT_INPUTS)) + (self.record.data[0x126],)

    def recalculate(self, *, level: int | None = None, promote: bool = False) -> None:
        """Calculate/heal atomically in memory; optional explicit stored conversion."""
        from .pa9_creation import recalculate_party
        updated = recalculate_party(self.record, level=level, promote=promote)
        self.record = updated
        self._calculated_signature = self._stat_signature()
        self.derived_stats_may_be_stale = False
        self._update_changed_fields()

    @property
    def dirty(self) -> bool:
        """New, never-saved documents also require a save/discard decision."""
        return self._new_unsaved or self.record.data != self._baseline

    def apply(self, changes: Mapping[str, object]) -> None:
        """Transactionally apply GUI/CLI changes to this document."""

        normalized: dict[str, object] = {}
        for raw_name, value in changes.items():
            name = normalize_field_name(raw_name)
            if name in normalized:
                raise ValueError(f"field was specified more than once: {name}")
            normalized[name] = value
        updated = apply_pa9_changes(self.record, normalized)
        if self.auto_recalculate:
            from .pa9_creation import recalculate_party, calculate_stats
            requested_level = None
            if updated.stat_level != self.record.stat_level:
                requested_level = updated.stat_level
                if updated.experience != self.record.experience:
                    if calculate_stats(updated)["stat_level"] != requested_level:
                        raise ValueError("level and experience changes conflict")
                    requested_level = None
            updated = recalculate_party(updated, level=requested_level)
        self.record = updated
        if self.auto_recalculate:
            self._calculated_signature = self._stat_signature()
        self._update_changed_fields()
        self.derived_stats_may_be_stale = bool(
            self.record.is_party
            and self._calculated_signature != self._stat_signature()
            and (self._baseline_derived_stats_may_be_stale
                 or self.changed_fields & DERIVED_STAT_INPUTS)
        )

    def _update_changed_fields(self) -> None:
        baseline = PA9(self._baseline, encrypted=False, validate=False)
        baseline_values = editor_values(baseline)
        current_values = editor_values(self.record)
        self.changed_fields = {
            spec.name
            for spec in EDITABLE_FIELDS
            if baseline_values[spec.name] != current_values[spec.name]
        }

    def reset(self) -> None:
        """Restore the last loaded or successfully saved canonical bytes."""

        self.record = PA9(self._baseline, encrypted=False, validate=False)
        self.changed_fields.clear()
        self._calculated_signature = None
        self.derived_stats_may_be_stale = self._baseline_derived_stats_may_be_stale

    def save(
        self,
        path: str | Path | None = None,
        *,
        overwrite: bool = False,
        expected_identity: FileIdentity | None | object = _UNSET_OUTPUT_IDENTITY,
    ) -> Path:
        """Atomically save, preserving stored/party size and choosing encryption by suffix.

        With no *path*, the loaded file is replaced only if its filesystem
        identity is unchanged.  A supplied path follows normal Save-As rules
        and requires ``overwrite=True`` when it already exists.  A UI or CLI
        that confirms an overwrite earlier can pass the result of
        :func:`snapshot_pa9_output_identity` as *expected_identity*.
        """

        if path is None:
            if expected_identity is not _UNSET_OUTPUT_IDENTITY:
                raise ValueError("expected_identity applies only to Save As")
            if self.path is None or self.source_identity is None:
                raise ValueError("this document has no source path; use Save As")
            target = _OutputTarget(self.path, self.source_identity)
            encrypted_for_path(target.path)
        else:
            requested = _absolute_path(path)
            if self.path is not None:
                same_path = os.path.normcase(os.fspath(requested)) == os.path.normcase(
                    os.fspath(self.path)
                )
                try:
                    same_file = os.path.samefile(requested, self.path)
                except OSError:
                    same_file = False
                if same_path or same_file:
                    raise ValueError(
                        "output refers to the loaded source; use in-place save/"
                        "--in-place so external-change protection remains enabled"
                    )
            target = _preflight_output(path, overwrite=overwrite)
            if expected_identity is not _UNSET_OUTPUT_IDENTITY:
                if expected_identity is not None and not isinstance(
                    expected_identity, FileIdentity
                ):
                    raise TypeError("expected_identity must be a FileIdentity or None")
                if target.identity != expected_identity:
                    raise FileExistsError(
                        "PA9 output changed after approval; refusing to overwrite: "
                        f"{target.path}"
                    )
        # Keep the target lock through the in-memory commit.  Otherwise a
        # second ZA-Trade writer could replace the just-verified file in the
        # small gap before this document records its new source identity.
        with _exclusive_save_lock(target):
            updated, identity = _atomic_write(
                self.record, target, acquire_lock=False
            )
            self.record = updated
            self.path = target.path
            self.input_encrypted = encrypted_for_path(target.path)
            self.source_identity = identity
            self._baseline = updated.data
            self._new_unsaved = False
            self._baseline_derived_stats_may_be_stale = (
                self.derived_stats_may_be_stale
            )
            self.changed_fields.clear()
        return target.path


def load_pa9_document(path: str | Path) -> PA9EditorDocument:
    """Convenience wrapper for :meth:`PA9EditorDocument.load`."""

    return PA9EditorDocument.load(path)


__all__ = [
    "EDITABLE_FIELDS",
    "FIELD_ALIASES",
    "FIELD_SPECS",
    "GENDER_NAMES_JA",
    "NATURE_NAMES_JA",
    "FileIdentity",
    "FieldSpec",
    "PA9EditorDocument",
    "ValidationIssue",
    "apply_pa9_changes",
    "editor_values",
    "encrypted_for_path",
    "file_identity",
    "load_pa9_document",
    "normalize_field_name",
    "pa9_editor_summary",
    "parse_assignments",
    "parse_editor_value",
    "snapshot_pa9_output_identity",
    "validate_editor_record",
]
