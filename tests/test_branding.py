"""Public name changes must not silently move saved preferences or wire identity."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from zasim import __version__
from zasim import i18n
from zasim.doctor import format_report
from zasim.gui_controller import ConnectionSettings
from zatrade import build_parser

ROOT = Path(__file__).resolve().parents[1]
NAME = "za-ldn-trade"


def test_distribution_and_entrypoint_names():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'name = "{NAME}"' in text
    assert f'{NAME} = "zatrade:main"' in text
    assert f'{NAME}-gui = "zasim.gui:launch_gui"' in text
    assert '\nzatrade = ' not in text and '\nzatrade-gui = ' not in text
    assert (ROOT / "Start-za-ldn-trade.cmd").is_file()
    assert not (ROOT / "Start-ZA-Trade.cmd").exists()


def test_cli_uses_product_name_in_help_and_version(capsys):
    parser = build_parser()
    assert parser.prog == NAME
    assert parser.format_help().startswith(f"usage: {NAME} ")
    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"{NAME} {__version__}\n"


def test_doctor_uses_product_name():
    report = {"version": __version__, "gui_ready": True, "python": "synthetic",
              "platform": "synthetic", "radios": [], "messages": []}
    assert format_report(report).splitlines()[0] == f"{NAME} {__version__}"


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_rename_retains_existing_preference_location(monkeypatch, tmp_path, platform):
    monkeypatch.setattr(i18n, "os", SimpleNamespace(name=platform, environ={
        "APPDATA": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)}))
    assert i18n.preference_path() == tmp_path / "za-trade-lab" / "preferences.json"


def test_rename_preserves_protocol_nickname_and_upstream_credit():
    assert ConnectionSettings().nickname == "ZA-Trade"
    for name in ("README.md", "README_JA.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert text.startswith(f"# {NAME}\n")
        assert "https://github.com/tornadus/frlg-ldn-trade" in text
        assert "AGPL-3.0-only" in text and "GPL-2.0-only" in text
