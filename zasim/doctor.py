"""Read-only installation checks, with an explicit local wireless detail mode."""
from __future__ import annotations

from .i18n import tr

import importlib.util
import os
from pathlib import Path
import platform
import shutil
import sys

from . import __version__
from .wireless import inventory_radios, inspect_wireless


def check_installation(*, backend="relay", sysfs=Path("/sys/class/net"),
                       radio=False, phyname=None):
    if backend not in ("relay", "direct"):
        raise ValueError("backend must be relay or direct")
    available = {
        name: importlib.util.find_spec(name) is not None
        for name in ("tkinter", "Crypto", "zstandard", "ldn", "trio")
    }
    gui_ready = sys.version_info >= (3, 10) and all(
        available[name] for name in ("tkinter", "Crypto", "zstandard")
    )
    tools = {name: shutil.which(name) is not None for name in
             ("iw", "ip", "nmcli", "sysctl", "pkill")}
    radios = []
    inventory_error = False
    if sys.platform.startswith("linux"):
        try:
            radios = inventory_radios(Path(sysfs))
        except OSError:
            inventory_error = True
    messages = []
    if inventory_error:
        messages.append(tr('無線一覧を読めません。機器が存在しないと確定したわけではありません。'))
    live_runtime_ready = (sys.platform.startswith("linux") and sys.version_info >= (3, 12)
                          and all(available[n] for n in ("Crypto", "zstandard", "ldn", "trio"))
                          and all(tools.values()))
    if not gui_ready:
        messages.append(tr('Python 3.10以上・Tk・requirements-za.txtの導入を確認してください。'))
    if backend == "relay":
        messages.append(tr('Relayは別途Linux側で起動します。このチェックは接続も無線操作もしません。'))
    else:
        if not sys.platform.startswith("linux") or sys.version_info < (3, 12):
            messages.append(tr('直接接続にはLinuxとPython 3.12以上が必要です。'))
        if not available["ldn"] or not available["trio"] or not all(tools.values()):
            messages.append(tr('Linux live依存（requirements.txt）と無線管理コマンドが必要です。'))
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            messages.append(tr('無線処理だけを管理者で実行し、通常ユーザーGUIはRelayに接続してください。'))
        if not radios:
            messages.append(tr('専用Wi-Fiが見つかりません。USB接続とドライバを確認してください。'))
    if any(r["driver"] == "rtw88_8812au" for r in radios):
        messages.append(
            tr('rtw88_8812auではドライバ/カーネルに依存する接続制約が確認されています。通常版の直接/Relay経路での動作は未保証です。対応状況はユーザーガイドを参照してください。')
        )
    wireless = inspect_wireless(radios=radios, phyname=phyname, sysfs=Path(sysfs)) if radio else None
    if inventory_error and wireless is not None:
        wireless["status"] = "unknown"
        wireless["issues"].append(tr('無線一覧が不完全なので肯定判定しません。'))
    return {"version": __version__, "platform": sys.platform,
            "python": platform.python_version(), "kernel": platform.release(),
            "gui_ready": gui_ready, "backend": backend, "live_runtime_ready": live_runtime_ready,
            "modules": available, "linux_tools": tools, "radios": radios,
            "wireless": wireless, "radio_inventory_complete": not inventory_error,
            "radio_mutated": False, "keys_read": False, "remote_relay_checked": False,
            "live_connection_tested": False, "messages": messages}


def exit_code(report):
    """0 is only prerequisites, never RF/LDN/physical exchange certification."""
    wireless = report.get("wireless")
    if wireless is None:
        return 0 if report["gui_ready"] else 2
    if wireless["status"] in ("unknown", "selection_required"):
        return 3
    if wireless["status"] != "requirements_present_not_verified":
        return 2
    return 0 if report["live_runtime_ready"] else 2


def format_report(report):
    lines = [f"za-ldn-trade {report['version']}",
             tr('GUIの依存: ') + ("OK" if report["gui_ready"] else tr('不足あり')),
             f"Python {report['python']} / {report['platform']}"]
    for radio in report["radios"]:
        lines.append(f"Wi-Fi: {radio['interface']} / {radio['phy']} / {radio['driver']}")
    lines += [""] + report["messages"]
    wireless = report.get("wireless")
    if wireless is not None:
        statuses = {
            "requirements_present_not_verified": tr('必要な機能表示あり／実通信は未検証'),
            "requirements_missing": tr('必要な機能の表示が不足（この構成では進めない）'),
            "in_use_or_blocked": tr('使用中または無効化中（自動変更しない）'),
            "not_found": tr('無線PHYが見つからない'),
            "selection_required": tr('複数PHYあり／明示選択が必要'),
            "linux_backend_required": tr('Linux/WSL側での検査が必要'),
            "unknown": tr('未判定（取得失敗・情報不足）'),
        }
        lines += ["", tr('無線詳細（このPCのみ）: ') + statuses[wireless["status"]],
                  tr('選択PHY: ') + str(wireless["selected_phy"] or tr('未選択')),
                  tr('Linux live依存: ') + ("OK" if report["live_runtime_ready"] else tr('不足あり'))]
        for key, value in wireless["required_checks"].items():
            lines.append("  " + key + ": " + (tr('表示あり') if value is True else tr('表示なし/制限あり') if value is False else tr('不明')))
        lines += wireless["issues"] + wireless["driver_notes"]
        lines.append(tr('これは読取り時点の必要条件。action frame送受信・接続・交換・復旧の成功証明ではありません。'))
    lines.append(tr('無線対応表: docs/WIRELESS_COMPATIBILITY.md（Relayサーバーは未診断）'))
    return "\n".join(lines)
