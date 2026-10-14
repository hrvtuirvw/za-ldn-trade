#!/usr/bin/env python3
"""Pokemon Legends: Z-A clean-room local-trade interoperability toolkit.

The toolkit decodes PA9 records and Pia 6.40 traffic, implements the five
recovered trade commands, and includes an experimental Linux LDN client plus
a loopback/SSH relay for running the upper protocol stack on Windows.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from zasim.compatibility import DEFAULT_ZA_TITLE_VERSION, SUPPORTED_ZA_TITLE_VERSIONS
from zasim.text import display_safe
from zasim import __version__


def _int_auto(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value}") from exc


def _hex_bytes(value: str, *, size: int | None = None) -> bytes:
    try:
        data = bytes.fromhex(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected hexadecimal bytes") from exc
    if size is not None and len(data) != size:
        raise argparse.ArgumentTypeError(f"expected {size} bytes ({size * 2} hex digits)")
    return data


def _json(value: object) -> None:
    # ASCII JSON remains printable even when a captured PA9 contains invalid
    # UTF-16 surrogate code units or the Windows console uses a legacy codec.
    print(json.dumps(value, ensure_ascii=True, indent=2))


def _stream_json_array(values) -> None:
    """Write an iterable as one JSON array without retaining every record."""

    first = True
    sys.stdout.write("[")
    for value in values:
        encoded = json.dumps(value, ensure_ascii=True, indent=2)
        sys.stdout.write(("\n" if first else ",\n") + encoded)
        first = False
    sys.stdout.write("\n]\n" if not first else "]\n")


def _output_path_identity(path: Path) -> str:
    """Return the publication identity without following the final path entry."""

    parent = path.parent.resolve(strict=False)
    return os.path.normcase(os.path.abspath(parent / path.name))


def _fsync_output_directory(path: Path) -> None:
    """Make a published directory entry durable where POSIX supports it."""

    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_binary_outputs(
    outputs: list[tuple[Path, bytes]], *, overwrite: bool
) -> None:
    """Preflight and atomically publish one or more binary CLI outputs."""

    if not outputs:
        return

    identities: set[str] = set()
    for target, _data in outputs:
        identity = _output_path_identity(target)
        if identity in identities:
            raise ValueError("--output and --wire-output must name different files")
        identities.add(identity)
        if not target.parent.is_dir():
            raise FileNotFoundError(f"output directory does not exist: {target.parent}")
        if target.is_dir():
            raise IsADirectoryError(f"output path is a directory: {target}")
        if (target.exists() or target.is_symlink()) and not overwrite:
            raise FileExistsError(
                f"output already exists: {target} (pass --force to replace it)"
            )

    staged: list[tuple[Path, Path]] = []
    try:
        for target, data in outputs:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            temporary = Path(temporary_name)
            staged.append((temporary, target))
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())

        published_directories: set[Path] = set()
        for temporary, target in staged:
            if overwrite:
                os.replace(temporary, target)
            else:
                # A same-directory hard link publishes the fully written inode
                # atomically and fails if a competing writer created the target.
                os.link(temporary, target)
                temporary.unlink()
            published_directories.add(target.parent.resolve(strict=False))
        for directory in published_directories:
            _fsync_output_directory(directory)
    finally:
        for temporary, _target in staged:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _reject_output_input_aliases(
    outputs: list[tuple[Path, bytes]], inputs: list[Path]
) -> None:
    """Keep an explicitly forced output from destroying a binary input file."""

    for target, _data in outputs:
        for source in inputs:
            aliases = _output_path_identity(target) == _output_path_identity(source)
            if not aliases and (target.exists() or target.is_symlink()):
                aliases = os.path.samefile(target, source)
            if aliases:
                raise ValueError(
                    f"output must be different from binary input file: {source}"
                )


_TRADE_COMMAND_IDS = {
    "ready": 0x100,
    "select": 0x101,
    "select-pokemon": 0x101,
    "confirm": 0x102,
    "confirm-trade": 0x102,
    "cancel": 0x103,
    "cancel-trade": 0x103,
    "final": 0x104,
    "final-agreement": 0x104,
}


def _trade_command_id(value: str) -> int:
    """Accept a command name, a 0--4 endpoint index, or a 0x100--0x104 ID."""
    normalized = value.strip().lower().replace("_", "-")
    if normalized in _TRADE_COMMAND_IDS:
        return _TRADE_COMMAND_IDS[normalized]
    try:
        numeric = int(normalized, 0)
    except ValueError as exc:
        raise ValueError(f"unknown trade command: {value}") from exc
    if 0 <= numeric <= 4:
        numeric += 0x100
    if not 0x100 <= numeric <= 0x104:
        raise ValueError("trade command must be ready/select/confirm/cancel/final, 0..4, or 0x100..0x104")
    return numeric


def _binary_arg(args: argparse.Namespace, prefix: str) -> bytes | None:
    from zasim.file_save import read_regular_bytes

    maximum = {"matrix": 0x4B0, "pa9": 0x158}.get(prefix, 65536)
    inline = getattr(args, f"{prefix}_hex", None)
    filename = getattr(args, f"{prefix}_file", None)
    if inline is not None:
        if len(inline) > maximum:
            raise ValueError(f"{prefix} binary input exceeds {maximum} bytes")
        return bytes(inline)
    if filename is not None:
        return read_regular_bytes(filename, max_size=maximum, allow_symlink=True)
    return None


def _offline_input(args: argparse.Namespace) -> bytes:
    value = _binary_arg(args, "input")
    if value is None:  # argparse enforces this; retain a useful API-level error.
        raise ValueError("one of --hex or --file is required")
    return value


def _pa9_info(mon, *, path: str | None = None, encrypted: bool | None = None) -> dict:
    info = {
        "path": path,
        "encrypted": encrypted,
        "encryption_constant": f"0x{mon.ec:08X}",
        "species_internal": mon.species_internal,
        "held_item": mon.item,
        "tid": mon.tid,
        "sid": mon.sid,
        "pid": f"0x{mon.pid:08X}",
        "nickname": display_safe(mon.nickname, limit=64),
        "checksum_valid": mon.checksum_valid,
    }
    return {key: value for key, value in info.items() if value is not None}


def cmd_profile(_args: argparse.Namespace) -> int:
    from zasim.profile import (
        ZA_BOX_TRADE_MAX_PARTICIPANTS,
        ZA_BOX_TRADE_SCENE_ID,
        ZA_PROFILE,
    )

    _json({
        "name": ZA_PROFILE.name,
        "title_id": ZA_PROFILE.title_id,
        "pia_version": ZA_PROFILE.pia_version,
        "pia_header_bytes": 29,
        "max_packet_size": ZA_PROFILE.max_packet_size,
        "box_trade_scene_id": ZA_BOX_TRADE_SCENE_ID,
        "box_trade_max_participants": ZA_BOX_TRADE_MAX_PARTICIPANTS,
        "application_communication_version": 1,
        "reliable_compact_identifier": {
            "protocol": 10,
            "port": 0,
            "protocol_specific": 0xFD,
        },
        "pa9_stored_bytes": 0x148,
        "pa9_party_bytes": 0x158,
        "source": "public interoperability documentation plus clean-room static analysis",
    })
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    from zasim.pa9 import PA9, is_encrypted_pa9
    from zasim.file_save import read_regular_bytes

    raw = read_regular_bytes(args.file, max_size=PA9.PARTY_SIZE, allow_symlink=True)
    encrypted = is_encrypted_pa9(raw)
    mon = PA9(raw, encrypted=encrypted, validate=not args.no_validate)
    _json(_pa9_info(mon, path=str(Path(args.file)), encrypted=encrypted))
    return 0


def cmd_list_species(_args: argparse.Namespace) -> int:
    from zasim.pa9_creation import species_catalog, STARTERS
    _json([{"national_id": p["species"], "form": p["form"],
            "internal_id": p["internal_id"], "name_ja": p["names"]["1"],
            "name_en": p["names"]["2"], "starter_preset": p["species"] in STARTERS and p["form"] == 0}
           for p in species_catalog()])
    return 0


def cmd_create_pa9(args: argparse.Namespace) -> int:
    from datetime import date
    from zasim.pa9_creation import CreateRequest, creation_summary
    from zasim.pa9_editor import PA9EditorDocument, snapshot_pa9_output_identity
    identity = snapshot_pa9_output_identity(args.output, overwrite=args.force)
    request = CreateRequest(
        species=args.species, form=args.form, level=args.level,
        ot_name=args.ot_name, tid=args.tid, sid=args.sid,
        encounter=args.encounter, language=args.language, ot_gender=args.ot_gender,
        met_date=date.fromisoformat(args.met_date) if args.met_date else None,
        met_location=args.met_location, met_level=args.met_level, ball=args.ball,
        nickname=args.nickname,
        moves=tuple(_int_auto(x.strip()) for x in args.moves.split(",")) if args.moves is not None else None,
        seed=args.seed,
    )
    document = PA9EditorDocument.create(request)
    output = document.save(args.output, overwrite=args.force, expected_identity=identity)
    _json({"output": str(output), **creation_summary(document.record, encounter=args.encounter)})
    return 0


def cmd_edit_pa9(args: argparse.Namespace) -> int:
    """Patch documented PA9 fields and atomically save the result."""

    from zasim.pa9_editor import (
        PA9EditorDocument,
        encrypted_for_path,
        pa9_editor_summary,
        parse_assignments,
        snapshot_pa9_output_identity,
        validate_editor_record,
    )

    if args.in_place and args.force:
        raise ValueError("--force does not apply with --in-place")
    approved_output_identity = None
    if not args.in_place:
        approved_output_identity = snapshot_pa9_output_identity(
            args.output, overwrite=args.force
        )
    document = PA9EditorDocument.load(args.input)
    input_encrypted = document.input_encrypted
    before = pa9_editor_summary(document.record)
    if not args.assignments and not args.recalculate and args.level is None and not args.party:
        raise ValueError("provide --set or --recalculate/--level/--party")
    changes = parse_assignments(args.assignments) if args.assignments else {}
    document.apply(changes)
    if args.recalculate or args.level is not None or args.party:
        document.recalculate(level=args.level, promote=args.party)
    changed_fields = sorted(document.changed_fields)
    stale_stats = document.derived_stats_may_be_stale
    if args.in_place:
        output = document.save()
    else:
        output = document.save(
            args.output,
            overwrite=args.force,
            expected_identity=approved_output_identity,
        )
    after = pa9_editor_summary(document.record)
    issues = validate_editor_record(document.record)
    _json(
        {
            "input": str(Path(args.input)),
            "input_encrypted": input_encrypted,
            "output": str(output),
            "output_encrypted": encrypted_for_path(output),
            "changed_fields": changed_fields,
            "derived_party_stats_may_be_stale": stale_stats,
            "before": before,
            "after": after,
            "issues": [
                {
                    "severity": issue.severity,
                    "field": issue.field,
                    "message": issue.message,
                }
                for issue in issues
            ],
            "legality": "not_checked",
        }
    )
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    from zasim.pa9 import scan_pa9_candidates

    raw = Path(args.file).read_bytes()
    candidates = scan_pa9_candidates(raw, record_size=args.record_size)
    rows = []
    for candidate in candidates:
        row = _pa9_info(candidate.pa9, encrypted=candidate.encrypted)
        row.update({"offset": candidate.offset, "record_size": candidate.size})
        rows.append(row)
    _json({"file": str(Path(args.file)), "count": len(rows), "candidates": rows})
    return 0


def cmd_decode(args: argparse.Namespace) -> int:
    from zasim.capture import decoded_summary
    from zasim.pia import decode_datagram

    datagram = _hex_bytes(args.datagram)
    ssid = _hex_bytes(args.ssid, size=16)
    decoded = decode_datagram(datagram, args.src_ip, ssid=ssid)
    _json(decoded_summary(decoded, preview=args.preview))
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    from zasim.capture import analyze_capture

    records = analyze_capture(args.capture, preview=args.preview)
    if args.packets_only:
        records = (
            record for record in records if record.get("type") == "packet"
        )
    _stream_json_array(records)
    return 0


def cmd_trade_encode(args: argparse.Namespace) -> int:
    """Encode one recovered application command, without sending it."""
    from zasim.capture import trade_command_summary
    from zasim.trade_protocol import (
        CancelTrade,
        CompatibilityMatrix,
        ConfirmTrade,
        FinalAgreement,
        Ready,
        SelectPokemon,
        encode_frame,
    )

    effective_id = _trade_command_id(args.trade_command)
    matrix = _binary_arg(args, "matrix")
    party_pa9 = _binary_arg(args, "pa9")

    if effective_id == 0x100:
        if matrix is None:
            raise ValueError("Ready requires --matrix-hex or --matrix-file (exactly 0x4B0 bytes)")
        if party_pa9 is not None or args.transient or args.reason is not None:
            raise ValueError("PA9, transient, and reason options do not apply to Ready")
        command = Ready(args.sequence, CompatibilityMatrix(matrix))
    elif effective_id == 0x101:
        if party_pa9 is None:
            raise ValueError("SelectPokemon requires --pa9-hex or --pa9-file (exactly 0x158 bytes)")
        if matrix is not None or args.reason is not None:
            raise ValueError("matrix and reason options do not apply to SelectPokemon")
        command = SelectPokemon(args.sequence, party_pa9, args.transient)
    elif effective_id == 0x102:
        if matrix is not None or party_pa9 is not None or args.transient or args.reason is not None:
            raise ValueError("binary, transient, and reason options do not apply to ConfirmTrade")
        command = ConfirmTrade(args.sequence)
    elif effective_id == 0x103:
        if matrix is not None or party_pa9 is not None or args.transient:
            raise ValueError("binary and transient options do not apply to CancelTrade")
        command = CancelTrade(args.sequence, args.reason if args.reason is not None else 0)
    else:
        if matrix is not None or party_pa9 is not None or args.transient or args.reason is not None:
            raise ValueError("binary, transient, and reason options do not apply to FinalAgreement")
        command = FinalAgreement(args.sequence)

    frame = encode_frame(command)
    result = {
        "offline_only": True,
        "command": trade_command_summary(command),
        "application_prefix": {
            "endpoint": frame.endpoint,
            "command_index": frame.command_index,
            "effective_id": f"0x{frame.effective_id:03X}",
            "descriptor_hex": frame.descriptor.hex(),
        },
        "archive_bytes": len(frame.archive_payload),
        "archive_hex": frame.archive_payload.hex(),
        "wire_payload_bytes": len(frame.wire_payload),
        "wire_payload_hex": frame.wire_payload.hex(),
    }
    outputs: list[tuple[Path, bytes]] = []
    output = Path(args.output) if args.output else None
    wire_output = Path(args.wire_output) if args.wire_output else None
    if output is not None:
        outputs.append((output, frame.archive_payload))
    if wire_output is not None:
        outputs.append((wire_output, frame.wire_payload))
    binary_inputs = [
        Path(filename)
        for filename in (args.matrix_file, args.pa9_file)
        if filename is not None
    ]
    _reject_output_input_aliases(outputs, binary_inputs)
    _atomic_write_binary_outputs(outputs, overwrite=args.force)

    if output is not None:
        result["output_file"] = str(output)
        result["output_kind"] = "archive"
    if wire_output is not None:
        result["wire_output_file"] = str(wire_output)
    _json(result)
    return 0


def cmd_trade_decode(args: argparse.Namespace) -> int:
    """Strictly decode one archive or prefixed Reliable application payload."""
    from zasim.capture import trade_command_summary
    from zasim.trade_protocol import TradeFrame, decode_frame

    effective_id = _trade_command_id(args.trade_command)
    payload = _offline_input(args)
    if args.input_kind == "wire-payload":
        frame = TradeFrame.from_wire_payload(payload)
        if frame.effective_id != effective_id:
            raise ValueError(
                f"wire payload command ID is 0x{frame.effective_id:03X}, "
                f"not requested 0x{effective_id:03X}"
            )
    else:
        frame = TradeFrame(effective_id, payload)
    command = decode_frame(frame)
    _json({
        "offline_only": True,
        "input_kind": args.input_kind,
        "application_prefix": {
            "endpoint": frame.endpoint,
            "command_index": frame.command_index,
            "effective_id": f"0x{frame.effective_id:03X}",
            "descriptor_hex": frame.descriptor.hex(),
        },
        "archive_bytes": len(frame.archive_payload),
        "wire_payload_bytes": len(frame.wire_payload),
        "command": trade_command_summary(command),
    })
    return 0


def cmd_trade_analyze(args: argparse.Namespace) -> int:
    """Best-effort analysis of an archive or Reliable application payload."""
    from zasim.capture import trade_archive_summary, trade_wire_payload_summary

    payload = _offline_input(args)
    if args.input_kind == "wire-payload":
        if args.command_index is not None or args.endpoint != 1:
            raise ValueError(
                "--endpoint/--command-index apply only to archive input; "
                "a wire payload already contains both bytes"
            )
        _json({
            "offline_only": True,
            "input_kind": "wire-payload",
            **trade_wire_payload_summary(payload),
        })
        return 0

    base = {
        "offline_only": True,
        "input_kind": "archive",
        "endpoint": args.endpoint,
        "archive_bytes": len(payload),
    }
    if args.command_index is not None:
        result = trade_archive_summary(args.endpoint, args.command_index, payload)
        _json({**base, **result})
        return 0
    if args.endpoint != 1:
        _json({
            **base,
            "recognized": False,
            "error": f"Z-A trade application frames use endpoint 1; got {args.endpoint}",
            "candidates": [],
        })
        return 0

    attempts = [trade_archive_summary(1, index, payload) for index in range(5)]
    candidates = [attempt["command"] for attempt in attempts if attempt["recognized"]]
    result = {**base, "recognized": bool(candidates), "candidates": candidates}
    if not candidates:
        result["errors"] = {
            str(attempt["command_index"]): attempt["error"] for attempt in attempts
        }
    elif len(candidates) > 1:
        result["ambiguous"] = True
    _json(result)
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from zasim.live import run_passive_probe

    summary = run_passive_probe(
        output=args.output,
        overwrite=args.force,
        keys_path=args.keys,
        relay=args.relay,
        duration=args.duration,
        nickname=args.nickname,
        phyname=args.phy,
        ifname=args.interface,
        comm_id=args.comm_id,
        scene_id=args.scene_id,
        link_code=args.link_code,
        single_interface=args.single_interface,
        **({"bss_helper": args.bss_helper} if getattr(args, "bss_helper", None) else {}),
    )
    _json(summary)
    return 0


def cmd_handshake(args: argparse.Namespace) -> int:
    """Exercise only generic Pia Net/Session/RTT, never BoxTrade."""
    from zasim.handshake import run_handshake_diagnostic

    def log(message: object) -> None:
        print(message, file=sys.stderr, flush=True)

    summary = run_handshake_diagnostic(
        output=args.output,
        overwrite=args.force,
        timeout=args.timeout,
        settle_grace=args.settle_grace,
        link_code=args.link_code,
        nickname=args.nickname,
        title_version=getattr(args, "title_version", DEFAULT_ZA_TITLE_VERSION),
        keys_path=args.keys,
        relay=args.relay,
        phyname=args.phy,
        ifname=args.interface,
        single_interface=args.single_interface,
        **({"bss_helper": args.bss_helper} if getattr(args, "bss_helper", None) else {}),
        log=log,
    )
    _json(summary)
    return 0 if summary["completed"] else 3


def cmd_trade(args: argparse.Namespace) -> int:
    """Run one BoxTrade including native peer setup and durable save sync."""
    from zasim.client import run_live_trade
    from zasim.pa9 import PA9
    from zasim.user_data import make_pc_user_data

    def log(message: object) -> None:
        print(message, file=sys.stderr, flush=True)

    def confirm(remote: bytes) -> bool:
        mon = PA9(remote, encrypted=True)
        print(
            "Remote offer: "
            f"species_internal={mon.species_internal}, "
            f"nickname={display_safe(mon.nickname, limit=64)!r}, "
            f"checksum_valid={mon.checksum_valid}",
            file=sys.stderr,
        )
        print(
            "Commit this trade? Type 'yes' to confirm: ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        answer = sys.stdin.readline()
        return answer.strip().lower() == "yes"

    result = run_live_trade(
        args.offer,
        peer_user_data=make_pc_user_data(args.nickname),
        sync_save=True,
        output=args.output,
        overwrite=args.force,
        auto_confirm=args.yes,
        confirmation_callback=None if args.yes else confirm,
        timeout=args.timeout,
        settle_grace=args.settle_grace,
        link_code=args.link_code,
        nickname=args.nickname,
        title_version=getattr(args, "title_version", DEFAULT_ZA_TITLE_VERSION),
        keys_path=args.keys,
        relay=args.relay,
        phyname=args.phy,
        ifname=args.interface,
        single_interface=args.single_interface,
        **({"bss_helper": args.bss_helper} if getattr(args, "bss_helper", None) else {}),
        log=log,
    )
    received = PA9(result.received_party_pa9, encrypted=True)
    _json({
        "experimental_live_path": True,
        "completed": result.completed,
        "native_save_sync_complete": result.native_save_sync_complete,
        "elapsed_seconds": result.elapsed,
        "output": str(result.output_path) if result.output_path is not None else None,
        "warning": result.warning,
        "received": _pa9_info(received, encrypted=True),
    })
    return 0 if result.completed else 3


def cmd_relay_serve(args: argparse.Namespace) -> int:
    """Own the Linux/WSL2 radio and relay Pia datagrams to Windows."""
    from zasim.relay import serve_za_relay

    def log(message: object) -> None:
        print(message, file=sys.stderr, flush=True)

    result = serve_za_relay(
        keys_path=args.keys,
        listen=args.listen,
        phyname=args.phy,
        ifname=args.interface,
        single_interface=args.single_interface,
        **({"bss_helper": args.bss_helper} if getattr(args, "bss_helper", None) else {}),
        once=args.once,
        log=log,
    )
    _json(result)
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    """Launch the Tk desktop client without importing Tk for other commands."""
    from zasim.gui import launch_gui

    return launch_gui(
        backend=args.backend,
        relay=args.relay,
        keys=args.keys,
        phyname=args.phy,
        ifname=args.interface,
        nickname=args.nickname,
        title_version=getattr(args, "title_version", DEFAULT_ZA_TITLE_VERSION),
        language=getattr(args, "language", None),
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check installation without reading keys or operating a radio."""
    from zasim.doctor import check_installation, format_report, exit_code
    from zasim.i18n import set_language, detect_language
    set_language(getattr(args, "language", None) or detect_language())
    if args.phy is not None and not args.radio:
        raise ValueError("doctor --phy requires --radio")
    report = check_installation(backend=args.backend, radio=args.radio, phyname=args.phy)
    if args.json:
        _json(report)
    else:
        print(format_report(report))
    return exit_code(report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="za-ldn-trade", description=__doc__)
    parser.add_argument("--version", action="version", version=f"za-ldn-trade {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="read-only installation and Wi-Fi inventory check (no keys/radio changes)")
    p.add_argument("--backend", choices=("relay", "direct"), default="relay")
    p.add_argument("--json", action="store_true", help="machine-readable result")
    p.add_argument("--radio", action="store_true", help="read local Linux radio prerequisites; no scanning/transmission/changes (0=prerequisites only, 2=missing/busy, 3=unknown/ambiguous)")
    p.add_argument("--phy", default=None, help="PHY to inspect; never auto-select among multiple radios")
    p.add_argument("--language", choices=("ja", "en"), help="report language (default: system locale)")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("profile", help="show the public Z-A transport profile")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser(
        "gui",
        help="launch the cross-platform Windows/Linux desktop client",
    )
    p.add_argument(
        "--backend",
        choices=("relay", "direct"),
        default=None,
        help="initial connection mode (default: this PC on Linux, prepared connection elsewhere)",
    )
    p.add_argument(
        "--relay",
        default="127.0.0.1:24872",
        help="initial loopback relay endpoint",
    )
    p.add_argument(
        "--keys",
        default="~/.switch/prod.keys",
        help="initial prod.keys path for Linux direct mode",
    )
    p.add_argument("--phy", default="", help="optional initial adapter selection; otherwise choose in the GUI")
    p.add_argument("--interface", default="ldnclient")
    p.add_argument("--nickname", default="ZA-Trade")
    p.add_argument(
        "--title-version", choices=SUPPORTED_ZA_TITLE_VERSIONS,
        default=DEFAULT_ZA_TITLE_VERSION,
        help="Z-A game version for the Join compatibility tag (default: 2.0.2)",
    )
    p.add_argument("--language", choices=("ja", "en"), help="UI language (default: saved preference or system locale)")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("inspect-pa9", help="inspect a stored or party PA9 file")
    p.add_argument("file")
    p.add_argument("--no-validate", action="store_true",
                   help="show fields even when structural validation fails")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("list-species", help="list creatable ZA National IDs/forms and starter presets")
    p.set_defaults(func=cmd_list_species)

    p = sub.add_parser("create-pa9", help="create a calculated party PA9 without any save/template",
        description="New full party PA9. starter = six documented gifts; custom = unverified encounter draft. No legality/game/trade guarantee.")
    p.add_argument("--species", type=_int_auto, required=True, help="NATIONAL Dex ID (not internal ID); see list-species")
    p.add_argument("--form", type=_int_auto, default=0)
    p.add_argument("--encounter", choices=("starter", "custom"), default="starter")
    p.add_argument("--level", type=_int_auto)
    p.add_argument("--ot-name", required=True)
    p.add_argument("--tid", type=_int_auto, required=True, help="16-bit TID, not displayed six-digit Trainer ID")
    p.add_argument("--sid", type=_int_auto, required=True, help="16-bit SID")
    p.add_argument("--ot-gender", type=int, choices=(0, 1), default=0)
    p.add_argument("--language", type=int, choices=(1,2,3,4,5,7,8,9,10,11), default=1)
    p.add_argument("--met-date", help="YYYY-MM-DD (default: local date)")
    p.add_argument("--met-location", type=_int_auto)
    p.add_argument("--met-level", type=_int_auto)
    p.add_argument("--ball", type=_int_auto, default=4)
    p.add_argument("--nickname")
    p.add_argument("--moves", help="1..4 comma-separated level/TM move IDs; default: encounter/level moves")
    p.add_argument("--seed", type=_int_auto, help="optional uint64 reproducibility seed; default: fresh random")
    p.add_argument("--output", required=True, help="new .pa9/.epa9/.ea9 target")
    p.add_argument("--force", action="store_true", help="explicitly approve replacement of existing output")
    p.set_defaults(func=cmd_create_pa9)

    p = sub.add_parser(
        "edit-pa9",
        help="edit documented PA9 fields and save atomically",
        description=(
            "Edit PA9 fields with repeatable --set FIELD=VALUE options. "
            "Numeric values accept decimal or 0x-prefixed hexadecimal. "
            "This performs structural checks, not full legality analysis."
        ),
    )
    p.add_argument("input", help="encrypted or decrypted 0x148/0x158 PA9 input")
    destination = p.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", help="Save-As target (.pa9/.epa9/.ea9)")
    destination.add_argument(
        "--in-place",
        action="store_true",
        help="atomically replace the input if it has not changed since loading",
    )
    p.add_argument(
        "--set",
        dest="assignments",
        action="append",
        required=False,
        metavar="FIELD=VALUE",
        help=(
            "field assignment; repeat as needed (examples: nickname=Z-A, "
            "species_internal=25, nature=3, iv_hp=31, is_alpha=true)"
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="replace an existing --output target (not used with --in-place)",
    )
    p.add_argument("--recalculate", action="store_true", help="calculate/heal party stats from public ZA data (not legality)")
    p.add_argument("--level", type=_int_auto, help="set level via the species growth curve, then calculate/heal")
    p.add_argument("--party", action="store_true", help="explicitly convert stored to calculated full party; never padding-only")
    p.set_defaults(func=cmd_edit_pa9)

    p = sub.add_parser("scan-pa9", help="find checksum-valid PA9 records in a binary capture")
    p.add_argument("file")
    p.add_argument("--record-size", type=_int_auto, choices=(0x148, 0x158), default=0x148,
                   help="record size (default: 0x148 stored; use 0x158 for party data)")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("decode", help="decode one Pia UDP datagram from hexadecimal input")
    p.add_argument("--ssid", required=True, help="16-byte LDN SSID as hex")
    p.add_argument("--src-ip", required=True, help="IPv4 address of the packet sender")
    p.add_argument("--datagram", required=True, help="full Pia UDP payload as hex")
    p.add_argument("--preview", type=int, default=48,
                   help="payload preview bytes per message")
    p.set_defaults(func=cmd_decode)

    p = sub.add_parser("analyze", help="decode a za-ldn-trade JSONL capture")
    p.add_argument("capture")
    p.add_argument("--preview", type=int, default=48,
                   help="payload preview bytes per message")
    p.add_argument("--packets-only", action="store_true")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("trade-encode", help="offline-encode a recovered trade command")
    p.add_argument("trade_command",
                   help="ready/select/confirm/cancel/final (also accepts index 0..4 or ID 0x100..0x104)")
    p.add_argument("--sequence", required=True, type=_int_auto)
    matrix_source = p.add_mutually_exclusive_group()
    matrix_source.add_argument("--matrix-hex", type=_hex_bytes,
                               help="Ready compatibility matrix as 0x4B0 bytes of hex")
    matrix_source.add_argument("--matrix-file",
                               help="file containing a 0x4B0-byte Ready compatibility matrix")
    pa9_source = p.add_mutually_exclusive_group()
    pa9_source.add_argument("--pa9-hex", type=_hex_bytes,
                            help="SelectPokemon party PA9 as 0x158 bytes of hex")
    pa9_source.add_argument("--pa9-file",
                            help="file containing a 0x158-byte encrypted party PA9")
    p.add_argument("--transient", action="store_true",
                   help="set SelectPokemon's recovered transient/clear Boolean")
    p.add_argument("--reason", type=_int_auto, default=None,
                   help="signed 32-bit CancelTrade reason (default: 0)")
    p.add_argument("--output", help="also write the archive-only bytes to this file")
    p.add_argument(
        "--wire-output",
        help="also write endpoint,index || archive (the Reliable application payload)",
    )
    p.add_argument(
        "--force", action="store_true", help="atomically replace existing output files"
    )
    p.set_defaults(func=cmd_trade_encode)

    p = sub.add_parser("trade-decode", help="offline-decode a known trade command archive")
    p.add_argument("trade_command",
                   help="ready/select/confirm/cancel/final (also accepts index 0..4 or ID 0x100..0x104)")
    input_source = p.add_mutually_exclusive_group(required=True)
    input_source.add_argument("--hex", dest="input_hex", type=_hex_bytes,
                              help="compact archive bytes as hex")
    input_source.add_argument("--file", dest="input_file",
                              help="file containing compact archive bytes")
    p.add_argument(
        "--input-kind",
        choices=("archive", "wire-payload"),
        default="archive",
        help="whether input is archive-only or endpoint,index || archive (default: archive)",
    )
    p.set_defaults(func=cmd_trade_decode)

    p = sub.add_parser(
        "trade-analyze",
        help="best-effort offline recognition of a trade archive or wire payload",
    )
    input_source = p.add_mutually_exclusive_group(required=True)
    input_source.add_argument("--hex", dest="input_hex", type=_hex_bytes,
                              help="compact archive bytes as hex")
    input_source.add_argument("--file", dest="input_file",
                              help="file containing the selected input kind")
    p.add_argument(
        "--input-kind",
        choices=("archive", "wire-payload"),
        default="archive",
        help="whether input is archive-only or endpoint,index || archive (default: archive)",
    )
    p.add_argument(
        "--endpoint", "--port", dest="endpoint", type=_int_auto, default=1,
        help="application endpoint for archive input (default: 1; --port is deprecated)",
    )
    p.add_argument(
        "--command-index", "--protocol-specific", dest="command_index", type=_int_auto,
        help="command index 0..4 for archive input; omit to try all layouts",
    )
    p.set_defaults(func=cmd_trade_analyze)

    p = sub.add_parser(
        "relay-serve",
        help="serve sequential Windows clients from a Linux/WSL2 LDN radio",
    )
    p.add_argument(
        "--listen",
        default="127.0.0.1:24872",
        help="IPv4 loopback endpoint only (default: 127.0.0.1:24872)",
    )
    p.add_argument(
        "--keys", required=True,
        help="Linux path passed to ldn.load_keys (never sent to Windows)",
    )
    p.add_argument("--phy", default="phy0")
    p.add_argument("--interface", default="ldnclient")
    p.add_argument(
        "--single-interface",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "reuse one physical netdev (default: auto-detect); a completely "
            "silent audited AWUS036ACH/rtw88 radio may be USB-reset once"
        ),
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="serve one client and exit instead of accepting sequential GUI/CLI sessions",
    )
    p.set_defaults(func=cmd_relay_serve)

    p = sub.add_parser(
        "probe",
        help="join and passively capture a Z-A LDN room (Linux or Windows relay)",
    )
    p.add_argument("--output", required=True)
    p.add_argument(
        "--force", action="store_true", help="replace an existing capture output"
    )
    connection = p.add_mutually_exclusive_group(required=True)
    connection.add_argument(
        "--keys",
        help="direct Linux path passed to ldn.load_keys (contents are never logged)",
    )
    connection.add_argument(
        "--relay",
        help="Windows relay endpoint after opening the SSH/WSL tunnel",
    )
    p.add_argument("--duration", type=float, default=30.0)
    p.add_argument("--nickname", default="ZATRADE")
    p.add_argument(
        "--link-code",
        required=True,
        metavar="8DIGITS",
        help="exact 8-digit Z-A communication password used to select the room",
    )
    p.add_argument("--phy", default="phy0")
    p.add_argument("--interface", default="ldnclient")
    p.add_argument(
        "--single-interface",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "reuse an existing physical netdev instead of creating LDN VIFs; "
            "auto-detected for 88XXau and PHYs without NEW_INTERFACE support; "
            "a completely silent audited AWUS036ACH/rtw88 radio may be "
            "USB-reset once"
        ),
    )
    p.add_argument("--comm-id", type=_int_auto, default=None)
    p.add_argument(
        "--scene-id", type=_int_auto, default=1,
        help="LDN scene (default: 1, the ROM's BoxTrade scene)",
    )
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser(
        "handshake",
        help="verify Z-A Net/Session/RTT without sending any BoxTrade command",
    )
    p.add_argument(
        "--output",
        help="optional JSONL capture of the generic handshake",
    )
    p.add_argument(
        "--force", action="store_true", help="replace an existing capture output"
    )
    connection = p.add_mutually_exclusive_group(required=True)
    connection.add_argument(
        "--keys",
        help="direct Linux path passed to ldn.load_keys (contents are never logged)",
    )
    connection.add_argument(
        "--relay",
        help="Windows relay endpoint after opening the SSH/WSL tunnel",
    )
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument(
        "--settle-grace",
        type=float,
        default=0.25,
        help="seconds to continue generic RTT pumping after success (default: 0.25)",
    )
    p.add_argument(
        "--link-code",
        required=True,
        metavar="8DIGITS",
        help="exact 8-digit Z-A communication password used to select the room",
    )
    p.add_argument("--nickname", default="ZA-Diag")
    p.add_argument("--phy", default="phy0", help="direct Linux mode only")
    p.add_argument("--interface", default="ldnclient", help="direct Linux mode only")
    p.add_argument(
        "--single-interface",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "reuse one physical netdev in direct mode (default: auto-detect); "
            "a completely silent audited AWUS036ACH/rtw88 radio may be "
            "USB-reset once"
        ),
    )
    p.add_argument(
        "--title-version", choices=SUPPORTED_ZA_TITLE_VERSIONS,
        default=DEFAULT_ZA_TITLE_VERSION,
        help="Z-A game version for the Join compatibility tag (default: 2.0.2)",
    )
    p.set_defaults(func=cmd_handshake)

    p = sub.add_parser(
        "trade",
        help="run active Z-A BoxTrade (Linux/root or Windows through relay)",
    )
    p.add_argument(
        "--offer", required=True,
        help="complete 0x158-byte party PA9 to offer (stored 0x148 is rejected)",
    )
    p.add_argument(
        "--output", required=True,
        help="save the received party PA9 (.epa9 keeps it encrypted)",
    )
    p.add_argument(
        "--force", action="store_true", help="replace an existing output file"
    )
    connection = p.add_mutually_exclusive_group(required=True)
    connection.add_argument(
        "--keys",
        help="direct Linux path passed to ldn.load_keys (contents are never logged)",
    )
    connection.add_argument(
        "--relay",
        help="Windows relay endpoint after opening the SSH/WSL tunnel",
    )
    p.add_argument(
        "--yes", action="store_true",
        help="confirm automatically; otherwise require typing 'yes' after seeing the offer",
    )
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument(
        "--settle-grace", type=float, default=5.0,
        help="seconds to keep pumping after final agreement (default: 5)",
    )
    p.add_argument(
        "--link-code",
        required=True,
        metavar="8DIGITS",
        help="exact 8-digit Z-A communication password used to select the room",
    )
    p.add_argument("--nickname", default="ZA-Trade")
    p.add_argument("--phy", default="phy0", help="direct Linux mode only")
    p.add_argument("--interface", default="ldnclient", help="direct Linux mode only")
    p.add_argument(
        "--single-interface",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "reuse one physical netdev in direct mode (default: auto-detect); "
            "a completely silent audited AWUS036ACH/rtw88 radio may be "
            "USB-reset once"
        ),
    )
    p.add_argument(
        "--title-version", choices=SUPPORTED_ZA_TITLE_VERSIONS,
        default=DEFAULT_ZA_TITLE_VERSION,
        help="Z-A game version for the Join compatibility tag (default: 2.0.2)",
    )
    p.set_defaults(func=cmd_trade)
    from zasim.bss_helper import command as helper_command
    helper = sub.add_parser("bss-helper", help="build/check the optional AWUS036ACH kernel helper (no loading)")
    actions = helper.add_subparsers(dest="helper_action", required=True)
    build = actions.add_parser("build", help="compile the bundled source against matching local kernel headers")
    build.add_argument("--output", required=True, help="new local bundle directory; no overwrite")
    build.add_argument("--kernel-build", help="trusted matching kernel build tree; default /lib/modules/$(uname -r)/build")
    build.set_defaults(func=helper_command)
    check = actions.add_parser("check", help="validate a local bundle without loading or radio access")
    check.add_argument("--bundle", required=True)
    check.set_defaults(func=helper_command)
    for name in ("probe", "handshake", "trade", "relay-serve"):
        sub.choices[name].add_argument("--bss-helper", metavar="BUNDLE",
            help="opt-in local BSS helper bundle for AWUS036ACH/rtw88_8812au; forces existing-interface reuse")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (ImportError, OSError, RuntimeError, ValueError, argparse.ArgumentTypeError) as exc:
        parser.exit(2, f"error: {display_safe(exc, limit=2048)}\n")


if __name__ == "__main__":
    raise SystemExit(main())
