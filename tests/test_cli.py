import io
import json

import pytest

import zatrade
from zasim.pa9 import PA9
from zasim.pia import PiaHeader, build_message, encode_datagram
from zasim.reliable import build_reliable
from zasim.trade_protocol import ConfirmTrade, encode_frame
from zatrade import build_parser, main


def test_profile_command(capsys):
    assert main(["profile"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["title_id"] == "0100F43008C44000"
    assert value["pia_version"] == "6.40"


@pytest.mark.parametrize(
    ("nickname", "message"),
    [("A" * 21, "20 characters"), ("😀" * 9, "32 UTF-8 bytes")],
)
def test_probe_cli_rejects_nickname_before_connecting(
    tmp_path, capsys, nickname: str, message: str
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(
            [
                "probe",
                "--output",
                str(tmp_path / "capture.jsonl"),
                "--relay",
                "127.0.0.1:24872",
                "--link-code",
                "12345678",
                "--nickname",
                nickname,
            ]
    )

    assert raised.value.code == 2
    assert message in capsys.readouterr().err


def test_inspect_and_scan_pa9_commands(tmp_path, capsys):
    mon = PA9()
    mon.ec = 0x12345678
    mon.species_internal = 25
    mon.nickname = "TEST"
    encrypted = mon.to_bytes(encrypted=True, party=False)
    pa9_path = tmp_path / "test.ea9"
    pa9_path.write_bytes(encrypted)

    assert main(["inspect-pa9", str(pa9_path)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["species_internal"] == 25
    assert inspected["nickname"] == "TEST"
    assert inspected["encrypted"] is True

    binary_path = tmp_path / "memory.bin"
    binary_path.write_bytes(b"prefix!" + encrypted + b"suffix")
    assert main(["scan-pa9", str(binary_path)]) == 0
    scanned = json.loads(capsys.readouterr().out)
    assert scanned["count"] == 1
    assert scanned["candidates"][0]["offset"] == 7


def test_edit_pa9_command_uses_shared_editor_and_output_suffix(tmp_path, capsys):
    mon = PA9()
    mon.species_internal = 25
    mon.nickname = "BEFORE"
    mon.nature = 0
    mon.stat_nature = 0
    mon.gender = 0
    mon.stat_level = 50
    mon.stat_hp_max = 100
    source = tmp_path / "source.epa9"
    source.write_bytes(mon.to_bytes(encrypted=True))
    output = tmp_path / "edited.pa9"

    assert main(
        [
            "edit-pa9",
            str(source),
            "--output",
            str(output),
            "--set",
            "nickname=AFTER",
            "--set",
            "species=0x85",
            "--set",
            "nature=13",
            "--set",
            "iv_hp=31",
            "--set",
            "is_alpha=true",
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["input_encrypted"] is True
    assert result["output_encrypted"] is False
    assert result["changed_fields"] == [
        "is_alpha",
        "iv_hp",
        "nature",
        "nickname",
        "species_internal",
    ]
    assert result["legality"] == "not_checked"
    edited = PA9.from_file(output)
    assert edited.nickname == "AFTER"
    assert edited.species_internal == 0x85
    assert edited.nature == 13
    assert edited.iv_hp == 31
    assert edited.is_alpha
    assert edited.checksum_valid


def test_edit_pa9_in_place_is_explicit_and_atomic(tmp_path, capsys):
    mon = PA9()
    mon.species_internal = 25
    mon.nickname = "OLD"
    source = tmp_path / "source.pa9"
    source.write_bytes(mon.to_bytes())

    assert main(
        ["edit-pa9", str(source), "--in-place", "--set", "nickname=NEW"]
    ) == 0
    capsys.readouterr()
    assert PA9.from_file(source).nickname == "NEW"


def test_edit_pa9_output_cannot_alias_input_even_with_force(tmp_path, capsys):
    mon = PA9()
    mon.species_internal = 25
    mon.nickname = "ORIGINAL"
    source = tmp_path / "source.pa9"
    original = mon.to_bytes()
    source.write_bytes(original)

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "edit-pa9",
                str(source),
                "--output",
                str(source),
                "--force",
                "--set",
                "nickname=CHANGED",
            ]
        )
    assert raised.value.code == 2
    assert "--in-place" in capsys.readouterr().err
    assert source.read_bytes() == original


def test_edit_pa9_force_pins_target_before_loading_input(
    tmp_path, monkeypatch, capsys
):
    from zasim.pa9_editor import PA9EditorDocument

    mon = PA9()
    mon.species_internal = 25
    mon.nickname = "SOURCE"
    source = tmp_path / "source.pa9"
    source.write_bytes(mon.to_bytes())
    target = tmp_path / "target.pa9"
    target.write_bytes(b"approved version")
    competing = b"changed while input was loading"
    real_load = PA9EditorDocument.load

    def load_then_race(cls, path):
        document = real_load(path)
        target.write_bytes(competing)
        return document

    monkeypatch.setattr(PA9EditorDocument, "load", classmethod(load_then_race))
    with pytest.raises(SystemExit) as raised:
        main(
            [
                "edit-pa9",
                str(source),
                "--output",
                str(target),
                "--force",
                "--set",
                "nickname=EDITED",
            ]
        )
    assert raised.value.code == 2
    assert "changed after approval" in capsys.readouterr().err
    assert target.read_bytes() == competing


def test_cli_error_escapes_control_characters(monkeypatch, capsys):
    def fail_with_hostile_text(_args):
        raise ValueError("hostile\x1b\nmessage")

    monkeypatch.setattr(zatrade, "cmd_inspect", fail_with_hostile_text)
    with pytest.raises(SystemExit) as raised:
        main(["inspect-pa9", "unused.pa9"])
    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert "\x1b" not in error
    assert error.count("\n") == 1
    assert "\\u001B" in error
    assert "\\u000A" in error


def test_decode_command(capsys):
    ssid = bytes(range(16))
    src_ip = "169.254.21.2"
    datagram = encode_datagram(
        build_message(10, b"hello"),
        src_ip,
        PiaHeader(packet_id=9, nonce=b"cli-test"),
        ssid=ssid,
    )
    assert main([
        "decode", "--ssid", ssid.hex(), "--src-ip", src_ip,
        "--datagram", datagram.hex(),
    ]) == 0
    decoded = json.loads(capsys.readouterr().out)
    assert decoded["header"]["packet_id"] == 9
    assert decoded["messages"][0]["proto"] == 10
    assert decoded["messages"][0]["payload_hex"] == b"hello".hex()


def test_decode_command_recognizes_trade_prefix_inside_reliable_payload(capsys):
    ssid = bytes(range(16))
    src_ip = "169.254.21.2"
    wire_payload = encode_frame(ConfirmTrade(42)).wire_payload
    datagram = encode_datagram(
        build_message(
            10,
            build_reliable(8, 0, wire_payload),
            # Compact metadata is independent of the title application prefix.
            port=9,
            protocol_specific=77,
        ),
        src_ip,
        PiaHeader(packet_id=10, nonce=b"tradecli"),
        ssid=ssid,
    )
    assert main([
        "decode", "--ssid", ssid.hex(), "--src-ip", src_ip,
        "--datagram", datagram.hex(),
    ]) == 0
    decoded = json.loads(capsys.readouterr().out)
    trade = decoded["messages"][0]["reliable"]["application_messages"][0]["trade"]
    assert trade["recognized"] is True
    assert trade["command"]["name"] == "confirm_trade"
    assert trade["command"]["sequence"] == 42


@pytest.mark.parametrize(
    ("name", "extra", "expected_name"),
    [
        ("ready", "matrix", "ready"),
        ("select", "pa9", "select_pokemon"),
        ("confirm", None, "confirm_trade"),
        ("cancel", "reason", "cancel_trade"),
        ("final", None, "final_agreement"),
    ],
)
def test_offline_trade_encode_decode_all_commands(
    name, extra, expected_name, tmp_path, capsys
):
    args = ["trade-encode", name, "--sequence", "7"]
    if extra == "matrix":
        matrix = tmp_path / "matrix.bin"
        matrix.write_bytes(bytes(0x4B0))
        args += ["--matrix-file", str(matrix)]
    elif extra == "pa9":
        party = tmp_path / "party.pa9"
        party.write_bytes(bytes((index * 17) & 0xFF for index in range(0x158)))
        args += ["--pa9-file", str(party), "--transient"]
    elif extra == "reason":
        args += ["--reason", "-2"]

    archive = tmp_path / f"{name}.bin"
    wire_payload = tmp_path / f"{name}.wire.bin"
    args += ["--output", str(archive), "--wire-output", str(wire_payload)]
    assert main(args) == 0
    encoded = json.loads(capsys.readouterr().out)
    assert encoded["offline_only"] is True
    assert encoded["command"]["name"] == expected_name
    assert bytes.fromhex(encoded["archive_hex"]) == archive.read_bytes()
    assert bytes.fromhex(encoded["wire_payload_hex"]) == wire_payload.read_bytes()
    assert wire_payload.read_bytes()[:2] == bytes((1, encoded["application_prefix"]["command_index"]))

    assert main(["trade-decode", name, "--file", str(archive)]) == 0
    decoded = json.loads(capsys.readouterr().out)
    assert decoded["command"]["name"] == expected_name
    assert decoded["command"]["sequence"] == 7
    if extra == "pa9":
        assert decoded["command"]["preview"] is True
        assert "sha256" in decoded["command"]["party_pa9"]
    if extra == "reason":
        assert decoded["command"]["reason"] == -2

    assert main([
        "trade-decode", name, "--input-kind", "wire-payload", "--file", str(wire_payload),
    ]) == 0
    decoded_wire = json.loads(capsys.readouterr().out)
    assert decoded_wire["input_kind"] == "wire-payload"
    assert decoded_wire["command"]["name"] == expected_name


def test_trade_encode_refuses_existing_outputs_without_partial_write(
    tmp_path, capsys
):
    archive = tmp_path / "new-archive.bin"
    wire_payload = tmp_path / "existing-wire.bin"
    original = b"do not replace"
    wire_payload.write_bytes(original)

    with pytest.raises(SystemExit) as error:
        main([
            "trade-encode", "confirm", "--sequence", "7",
            "--output", str(archive), "--wire-output", str(wire_payload),
        ])

    assert error.value.code == 2
    assert not archive.exists()
    assert wire_payload.read_bytes() == original
    assert "--force" in capsys.readouterr().err


def test_trade_encode_force_atomically_replaces_existing_outputs(tmp_path, capsys):
    archive = tmp_path / "archive.bin"
    wire_payload = tmp_path / "wire.bin"
    archive.write_bytes(b"old archive")
    wire_payload.write_bytes(b"old wire")

    assert main([
        "trade-encode", "confirm", "--sequence", "7", "--force",
        "--output", str(archive), "--wire-output", str(wire_payload),
    ]) == 0

    encoded = json.loads(capsys.readouterr().out)
    assert archive.read_bytes() == bytes.fromhex(encoded["archive_hex"])
    assert wire_payload.read_bytes() == bytes.fromhex(encoded["wire_payload_hex"])
    assert not list(tmp_path.glob(".*.tmp"))


def test_trade_encode_rejects_colliding_output_paths_before_writing(
    tmp_path, capsys
):
    target = tmp_path / "same.bin"

    with pytest.raises(SystemExit) as error:
        main([
            "trade-encode", "confirm", "--sequence", "7",
            "--output", str(target), "--wire-output", str(target),
        ])

    assert error.value.code == 2
    assert not target.exists()
    assert "different files" in capsys.readouterr().err


def test_trade_encode_no_clobber_wins_race(tmp_path, monkeypatch, capsys):
    target = tmp_path / "archive.bin"
    competing = b"competing writer"
    real_link = zatrade.os.link

    def racing_link(source, destination):
        target.write_bytes(competing)
        return real_link(source, destination)

    monkeypatch.setattr(zatrade.os, "link", racing_link)
    with pytest.raises(SystemExit) as error:
        main([
            "trade-encode", "confirm", "--sequence", "7",
            "--output", str(target),
        ])

    assert error.value.code == 2
    assert target.read_bytes() == competing
    assert not list(tmp_path.glob(".*.tmp"))
    capsys.readouterr()


@pytest.mark.parametrize(
    ("command", "input_option", "size"),
    [("ready", "--matrix-file", 0x4B0), ("select", "--pa9-file", 0x158)],
)
def test_trade_encode_force_never_replaces_its_binary_input(
    command, input_option, size, tmp_path, capsys
):
    source = tmp_path / "source.bin"
    original = bytes((index * 13) & 0xFF for index in range(size))
    source.write_bytes(original)

    with pytest.raises(SystemExit) as error:
        main([
            "trade-encode", command, "--sequence", "7",
            input_option, str(source), "--output", str(source), "--force",
        ])

    assert error.value.code == 2
    assert source.read_bytes() == original
    assert "binary input" in capsys.readouterr().err


def test_trade_analyze_reports_ambiguity_and_malformed_input(capsys):
    assert main(["trade-analyze", "--hex", "b90100"]) == 0
    analyzed = json.loads(capsys.readouterr().out)
    assert analyzed["recognized"] is True
    assert analyzed["ambiguous"] is True
    assert [row["name"] for row in analyzed["candidates"]] == [
        "confirm_trade",
        "final_agreement",
    ]

    assert main([
        "trade-analyze", "--protocol-specific", "3", "--hex", "deadbeef",
    ]) == 0
    malformed = json.loads(capsys.readouterr().out)
    assert malformed["recognized"] is False
    assert "error" in malformed

    assert main([
        "trade-analyze", "--input-kind", "wire-payload", "--hex", "0102b9012a",
    ]) == 0
    wire = json.loads(capsys.readouterr().out)
    assert wire["recognized"] is True
    assert wire["endpoint"] == 1
    assert wire["command_index"] == 2
    assert wire["command"]["sequence"] == 42


def test_trade_decode_accepts_hex_and_numeric_endpoint_index(capsys):
    assert main(["trade-decode", "2", "--hex", "b90109"]) == 0
    decoded = json.loads(capsys.readouterr().out)
    assert decoded["application_prefix"]["effective_id"] == "0x102"
    assert decoded["command"]["sequence"] == 9


def test_trade_cli_calls_live_driver_only_with_explicit_inputs(
    monkeypatch, tmp_path, capsys
):
    from zasim.client import LiveTradeResult

    encrypted_party = PA9().to_bytes(encrypted=True, party=True)
    received_path = tmp_path / "received.pa9"
    called = {}

    def fake_trade(offer, **kwargs):
        called["offer"] = offer
        called.update(kwargs)
        return LiveTradeResult(encrypted_party, 1.25, received_path)

    monkeypatch.setattr("zasim.client.run_live_trade", fake_trade)
    assert main([
        "trade",
        "--offer", str(tmp_path / "offer.pa9"),
        "--output", str(received_path),
        "--keys", str(tmp_path / "prod.keys"),
        "--link-code", "12345678",
        "--yes",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["completed"] is True
    assert result["output"] == str(received_path)
    assert called["auto_confirm"] is True
    assert called["confirmation_callback"] is None
    assert called["keys_path"].endswith("prod.keys")
    assert called["link_code"] == "12345678"


def test_trade_cli_returns_3_for_indeterminate_postcommit_result(
    monkeypatch, tmp_path, capsys
) -> None:
    from zasim.client import LiveTradeResult

    party = PA9().to_bytes(encrypted=True, party=True)
    received_path = tmp_path / "received.epa9"

    monkeypatch.setattr(
        "zasim.client.run_live_trade",
        lambda *_args, **_kwargs: LiveTradeResult(
            party,
            0.5,
            received_path,
            completed=False,
            warning="ReliableTimeout occurred after local FinalAgreement",
        ),
    )

    assert main([
        "trade",
        "--offer", str(tmp_path / "offer.epa9"),
        "--output", str(received_path),
        "--relay", "127.0.0.1:24872",
        "--link-code", "12345678",
        "--yes",
    ]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["completed"] is False
    assert "ReliableTimeout" in result["warning"]


def test_trade_cli_selects_windows_relay_without_a_windows_key_path(
    monkeypatch, tmp_path, capsys
):
    from zasim.client import LiveTradeResult

    received_path = tmp_path / "received.epa9"
    party = PA9().to_bytes(encrypted=True, party=True)
    called = {}

    def fake_trade(offer, **kwargs):
        called["offer"] = offer
        called.update(kwargs)
        return LiveTradeResult(party, 0.5, received_path)

    monkeypatch.setattr("zasim.client.run_live_trade", fake_trade)
    assert main([
        "trade",
        "--offer", str(tmp_path / "offer.epa9"),
        "--output", str(received_path),
        "--relay", "127.0.0.1:24872",
        "--link-code", "12345678",
        "--yes",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["completed"] is True
    assert called["keys_path"] is None
    assert called["relay"] == "127.0.0.1:24872"
    assert called["link_code"] == "12345678"


@pytest.mark.parametrize(("answer", "approved"), [("yes\n", True), ("no\n", False)])
def test_trade_cli_manual_confirmation_policy(
    monkeypatch, tmp_path, capsys, answer: str, approved: bool
) -> None:
    from zasim.client import LiveTradeResult

    remote = PA9().to_bytes(encrypted=True, party=True)
    received_path = tmp_path / "received.epa9"
    decisions = []

    def fake_trade(_offer, **kwargs):
        decisions.append(kwargs["confirmation_callback"](remote))
        return LiveTradeResult(remote, 0.5, received_path)

    monkeypatch.setattr("zasim.client.run_live_trade", fake_trade)
    monkeypatch.setattr("sys.stdin", io.StringIO(answer))
    assert main([
        "trade",
        "--offer", str(tmp_path / "offer.epa9"),
        "--output", str(received_path),
        "--relay", "127.0.0.1:24872",
        "--link-code", "12345678",
    ]) == 0
    capsys.readouterr()
    assert decisions == [approved]


def test_probe_and_relay_serve_cli_routing(monkeypatch, tmp_path, capsys) -> None:
    probe_called = {}
    serve_called = {}

    def fake_probe(**kwargs):
        probe_called.update(kwargs)
        return {"output": kwargs["output"], "packets": 0}

    def fake_serve(**kwargs):
        serve_called.update(kwargs)
        return {"listen": kwargs["listen"], "packets_to_windows": 0}

    monkeypatch.setattr("zasim.live.run_passive_probe", fake_probe)
    monkeypatch.setattr("zasim.relay.serve_za_relay", fake_serve)

    capture = tmp_path / "capture.jsonl"
    assert main([
        "probe", "--output", str(capture),
        "--relay", "127.0.0.1:24872",
        "--link-code", "12345678",
    ]) == 0
    json.loads(capsys.readouterr().out)
    assert probe_called["keys_path"] is None
    assert probe_called["relay"] == "127.0.0.1:24872"
    assert probe_called["link_code"] == "12345678"

    assert main([
        "relay-serve", "--keys", "/home/user/prod.keys",
        "--listen", "127.0.0.1:25000",
    ]) == 0
    json.loads(capsys.readouterr().out)
    assert serve_called["keys_path"] == "/home/user/prod.keys"
    assert serve_called["listen"] == "127.0.0.1:25000"


def test_gui_cli_routes_lazily_without_starting_tk(monkeypatch) -> None:
    called = {}

    def fake_launch(**kwargs):
        called.update(kwargs)
        return 0

    monkeypatch.setattr("zasim.gui.launch_gui", fake_launch)
    assert main([
        "gui",
        "--backend", "relay",
        "--relay", "127.0.0.1:25001",
        "--nickname", "GUI-Test",
    ]) == 0
    assert called["backend"] == "relay"
    assert called["relay"] == "127.0.0.1:25001"
    assert called["nickname"] == "GUI-Test"


def test_live_cli_requires_exactly_one_local_or_relay_backend(tmp_path) -> None:
    parser = build_parser()
    base = [
        "trade", "--offer", str(tmp_path / "offer.epa9"),
        "--output", str(tmp_path / "received.epa9"),
        "--link-code", "12345678",
    ]
    with pytest.raises(SystemExit):
        parser.parse_args(base)
    with pytest.raises(SystemExit):
        parser.parse_args(
            base + [
                "--keys", "/home/user/prod.keys",
                "--relay", "127.0.0.1:24872",
            ]
        )
