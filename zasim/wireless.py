"""Bounded, read-only Linux LDN prerequisites; never a radio/transaction test.

Only local GET_WIPHY/GETFAMILY netlink requests, `iw phy NAME info`, and
`ip -j ... show` are used. No scan, connection, frame, key, rfkill write,
module load, sudo, interface change or remote relay access is performed.
"""
from __future__ import annotations

from .i18n import tr

import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import time

SOCIAL_CHANNELS = {1: 2412, 6: 2437, 11: 2462}
PHY_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z")
LEGACY_DRIVERS = frozenset(("88xxau", "8812au", "rtl8812au", "rtl88xxau"))


def inventory_radios(sysfs: Path) -> list[dict]:
    """No commands or file-content reads in the basic installation check."""
    radios = []
    for entry in sorted(sysfs.iterdir()):
        phy = entry / "phy80211"
        if not phy.exists():
            continue
        driver = entry / "device/driver/module"
        radios.append({"interface": entry.name, "phy": phy.resolve().name,
                       "driver": driver.resolve().name if driver.exists() else "unknown"})
    return radios


def _small(path: Path) -> str | None:
    try:
        with path.open("r", encoding="ascii", errors="strict") as stream:
            text = stream.read(256)
        return text.strip() if len(text) < 256 else None
    except (OSError, UnicodeError):
        return None


def _command(argv: list[str]) -> str:
    # This is an allowlist, not a general-purpose shell runner.
    allowed = argv in (["ip", "-j", "-4", "route", "show", "table", "all"],
                       ["ip", "-j", "-6", "route", "show", "table", "all"],
                       ["ip", "-j", "address", "show"])
    allowed |= (len(argv) == 4 and argv[:2] == ["iw", "phy"]
                and bool(PHY_NAME.fullmatch(argv[2])) and argv[3] == "info")
    if not allowed:
        raise ValueError("not an allowed read-only wireless query")
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                            errors="replace", timeout=3, check=False,
                            env={**os.environ, "LC_ALL": "C"})
    if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
        raise OSError("local read-only command failed or exceeded output limit")
    return result.stdout


def _section(text: str, heading: str) -> list[str] | None:
    lines = text.expandtabs(8).splitlines()
    found = None
    for line in lines:
        if line.strip() == heading:
            found = (len(line) - len(line.lstrip()), [])
        elif found is not None:
            indent, values = found
            if line.strip() and len(line) - len(line.lstrip()) <= indent:
                break
            if line.strip():
                values.append(line.strip())
    return None if found is None else found[1]


def parse_iw_phy(text: str, phyname: str) -> dict:
    """Parse only a single, identity-matched Wiphy; other PHYs cannot lend bits."""
    if re.findall(r"^Wiphy (\S+)\s*$", text, re.M) != [phyname]:
        raise ValueError("selected Wiphy identity missing or ambiguous")
    modes = _section(text, "Supported interface modes:")
    ciphers = _section(text, "Supported Ciphers:")
    features = _section(text, "Supported extended features:")
    commands = _section(text, "Supported commands:")
    over_nl = None if features is None else any(re.fullmatch(
        r"\*\s+\[\s*CONTROL_PORT_OVER_NL80211\s*\](?:\s*:.*)?", line
    ) for line in features)
    channel_rows = {}
    for freq, channel, flags in re.findall(
        r"^\s*\*\s+(\d+(?:\.0+)?) MHz \[(\d+)\]([^\n]*)$", text, re.M
    ):
        n = int(channel)
        if n in SOCIAL_CHANNELS and int(freq.partition(".")[0]) == SOCIAL_CHANNELS[n]:
            if n in channel_rows:
                raise ValueError("duplicate selected channel")
            channel_rows[n] = flags.lower()
    # Current public transport discovers on 1/6/11, not all possible 5GHz bands.
    channels = {}
    for n in SOCIAL_CHANNELS:
        flags = channel_rows.get(n)
        channels[str(n)] = (None if not channel_rows and "Frequencies:" not in text
                            else flags is not None and not any(
                                x in flags for x in ("disabled", "no ir", "no-ir", "radar", "no ibss")))
    return {"managed": None if modes is None else any(re.fullmatch(r"\*\s+managed", line) for line in modes),
            "monitor": None if modes is None else any(re.fullmatch(r"\*\s+monitor", line) for line in modes),
            "ccmp": None if ciphers is None else any(re.fullmatch(
                r"\*\s+CCMP(?:-128)?\s+\(00-0f-ac:4\)", line) for line in ciphers),
            "control_port_over_nl80211": over_nl,
            "social_channels": channels,
            "new_interface_advertised": None if commands is None else "* new_interface" in commands}


# Stable Linux UAPI constants (linux/{netlink,genetlink,nl80211}.h).
# iw does NOT print NL80211_ATTR_CONTROL_PORT_ETHERTYPE. Do not infer it
# from the distinct EXT_FEATURES bit 26. Query the flag itself.
def _attribute(kind: int, payload: bytes) -> bytes:
    length = 4 + len(payload)
    return struct.pack("=HH", length, kind) + payload + bytes((-length) % 4)


def _attributes(payload: bytes) -> dict[int, bytes]:
    result = {}
    while payload:
        if len(payload) < 4:
            raise ValueError("truncated netlink attribute")
        length, kind = struct.unpack_from("=HH", payload)
        kind &= 0x3fff
        if length < 4 or length > len(payload) or kind in result:
            raise ValueError("invalid/duplicate netlink attribute")
        result[kind] = payload[4:length]
        payload = payload[(length + 3) & ~3:]
    return result


def _get(sock, family: int, command: int, attribute: bytes, sequence: int,
         deadline: float) -> list[dict[int, bytes]]:
    body = struct.pack("=BBH", command, 1, 0) + attribute
    # REQUEST|ACK only. No dump, subscriptions, SET or wireless frame commands.
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("read-only netlink deadline")
    sock.settimeout(remaining)
    sock.sendto(struct.pack("=IHHII", 16 + len(body), family, 5, sequence, 0) + body, (0, 0))
    rows = []
    total = 0
    for _ in range(64):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("read-only netlink deadline")
        sock.settimeout(remaining)
        data, _ancillary, flags, sender = sock.recvmsg(65536)
        total += len(data)
        if sender[0] != 0 or flags & socket.MSG_TRUNC or total > 1024 * 1024:
            raise ValueError("invalid netlink source or truncated response")
        while data:
            if len(data) < 16:
                raise ValueError("truncated netlink header")
            length, kind, msg_flags, seq, _pid = struct.unpack_from("=IHHII", data)
            if length < 16 or length > len(data) or seq != sequence or msg_flags & 0x10:
                raise ValueError("invalid/interrupted netlink response")
            body = data[16:length]
            data = data[(length + 3) & ~3:]
            if kind == 2:  # NLMSG_ERROR also contains successful ACKs (error=0)
                if len(body) < 4:
                    raise ValueError("truncated netlink ACK")
                error = struct.unpack_from("=i", body)[0]
                if error:
                    raise OSError(-error, "read-only netlink request failed")
                if not rows:
                    raise ValueError("ACK without a Wiphy/family response")
                return rows
            if kind != family or len(body) < 4 or body[0] != (1 if family == 16 else 3):
                raise ValueError("unexpected netlink response type/command")
            rows.append(_attributes(body[4:]))
    raise ValueError("netlink message limit")


def read_control_port(phyname: str, index: int) -> dict:
    """Local kernel query only; no CAP_NET_ADMIN, ldn import, or RF traffic."""
    if not PHY_NAME.fullmatch(phyname) or type(index) is not int or not 0 <= index < 2**32:
        raise ValueError("invalid Wiphy identity")
    deadline = time.monotonic() + 3
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 16) as sock:
        sock.bind((0, 0))
        families = _get(sock, 16, 3, _attribute(2, b"nl80211\0"), 1, deadline)
        if len(families) != 1 or families[0].get(2) != b"nl80211\0" or len(families[0].get(1, b"")) != 2:
            raise ValueError("wrong generic-netlink family")
        family = struct.unpack("=H", families[0][1])[0]
        if family <= 18:
            raise ValueError("invalid generic-netlink family ID")
        rows = _get(sock, family, 1, _attribute(1, struct.pack("=I", index)), 2, deadline)
        if len(rows) != 1:
            raise ValueError("ambiguous non-split Wiphy response")
        row = rows[0]
        if row.get(1) != struct.pack("=I", index) or row.get(2) != phyname.encode() + b"\0":
            raise ValueError("Wiphy changed during inspection")
        ext = row.get(217)
        return {"custom_control_port_ethertype": 102 in row,
                "control_port_over_nl80211": None if ext is None else len(ext) > 3 and bool(ext[3] & 4)}


def driver_note(driver: str) -> str:
    name = driver.lower()
    if name in LEGACY_DRIVERS:
        return tr('legacy 88XXau系はLDN認証非対応の実績。monitor/injectionだけでは不足。')
    if name in ("rtw88_8812au", "rtw_8812au"):
        return tr('ACH/RTL8812AU: 実機交換成功は開発用BSS補助経路。配布標準経路は未検証。')
    if name == "mt76x0u":
        return tr('ACHM/MT7610U: 上流FRLGで高信頼の報告。Z-A標準経路では未検証。')
    if name == "rtw88_8821ce":
        return tr('RTL8821CE: 上流FRLGで高信頼の報告。Z-A標準経路では未検証。')
    if name == "mt7921e":
        return tr('RZ616は上流FRLGで低信頼・停止報告あり。同driverの全機種を検証したものではない。')
    if name in ("iwlwifi", "ath9k_htc"):
        return tr('上流FRLGでAX200/AR9271のIP割当て問題あり。同driverの全機種が非対応とは断定しない。')
    return tr('このdriverのZ-A標準経路での実機交換実績は未登録。機能表示だけでは動作保証しない。')


def _safety(sysfs: Path, phys: Path, phyname: str, interfaces: set[str]) -> dict:
    defaults = set()
    for family in ("-4", "-6"):
        rows = json.loads(_command(["ip", "-j", family, "route", "show", "table", "all"]))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("invalid route inventory")
        for row in rows:
            if row.get("dst") in ("default", "0.0.0.0/0", "::/0"):
                defaults.add(row.get("dev"))
                hops = row.get("nexthops", [])
                if not isinstance(hops, list) or any(not isinstance(hop, dict) for hop in hops):
                    raise ValueError("invalid multipath route")
                defaults.update(hop.get("dev") for hop in hops)
    addresses = json.loads(_command(["ip", "-j", "address", "show"]))
    if not isinstance(addresses, list) or any(not isinstance(row, dict) for row in addresses):
        raise ValueError("invalid interface inventory")
    found = {row["ifname"] for row in addresses}
    if not interfaces <= found:
        raise ValueError("interface disappeared during inspection")
    has_ip = any(row["ifname"] in interfaces and row.get("addr_info") for row in addresses)
    carrier = False
    for name in interfaces:
        value = _small(sysfs / name / "carrier")
        if value not in ("0", "1"):
            # Linux can return EINVAL for carrier on an administratively DOWN
            # netdev. Prove DOWN rather than treating every read error as idle.
            flags = _small(sysfs / name / "flags")
            if flags is None or not re.fullmatch(r"0x[0-9a-fA-F]+", flags) or int(flags, 16) & 1:
                raise ValueError("carrier state unavailable")
        carrier |= value == "1"
    blocked = False
    for rf in (phys / phyname).glob("rfkill*"):
        soft, hard = _small(rf / "soft"), _small(rf / "hard")
        if soft not in ("0", "1") or hard not in ("0", "1"):
            raise ValueError("rfkill state unavailable")
        blocked |= soft == "1" or hard == "1"
    return {"default_route_on_phy": bool(interfaces & defaults),
            "ip_addresses_on_phy": bool(has_ip), "carrier_on_phy": carrier,
            "rfkill_blocked": blocked}


def inspect_wireless(*, radios: list[dict], phyname: str | None = None,
                     sysfs=Path("/sys/class/net")) -> dict:
    """Inspect one explicit/unambiguous local PHY. Never choose among radios."""
    report = {"status": "unknown", "selected_phy": phyname, "required_checks": {},
              "safety": {}, "issues": [], "driver_notes": [],
              "action_frame_io_tested": False, "standard_exchange_verified": False}
    if not sys.platform.startswith("linux"):
        report["status"] = "linux_backend_required"
        report["issues"].append(tr('Windowsの仮想Ethernetでは無線能力を判定できません。Linux/WSL側で実行してください。'))
        return report
    phys = Path(sysfs).parent / "ieee80211"
    names = {row["phy"] for row in radios}
    try:
        names.update(p.name for p in phys.iterdir() if p.is_dir())
    except FileNotFoundError:
        pass
    except OSError:
        report["issues"].append(tr('無線PHY一覧を読めません。'))
        return report
    if phyname is not None and not PHY_NAME.fullmatch(phyname):
        report["issues"].append(tr('PHY名が不正です。現在の一覧から指定してください。'))
        return report
    if not names or (phyname is not None and phyname not in names):
        report["status"] = "not_found"
        report["issues"].append(tr('指定した無線PHYが見つかりません。接続・driver・VM/WSLへのUSB割当てを確認してください。'))
        return report
    if phyname is None:
        if len(names) != 1:
            report["status"] = "selection_required"
            report["issues"].append(tr('複数PHYがあります。専用機を --phy NAME で指定してください（自動選別しません）。'))
            return report
        phyname = next(iter(names))
    report["selected_phy"] = phyname
    selected = [row for row in radios if row["phy"] == phyname]
    report["driver_notes"] = [driver_note(driver) for driver in sorted({r["driver"] for r in selected})]
    checks = report["required_checks"]
    try:
        parsed = parse_iw_phy(_command(["iw", "phy", phyname, "info"]), phyname)
        checks.update({k: parsed[k] for k in ("managed", "monitor", "ccmp", "control_port_over_nl80211")})
        checks.update({"channel_" + k: v for k, v in parsed["social_channels"].items()})
        report["new_interface_advertised"] = parsed["new_interface_advertised"]
    except (OSError, ValueError, subprocess.SubprocessError):
        report["issues"].append(tr('選択PHYのiw情報を取得・解析できません。iw/権限/driverを確認してください。'))
        for key in ("managed", "monitor", "ccmp", "control_port_over_nl80211", "channel_1", "channel_6", "channel_11"):
            checks[key] = None
    try:
        index = _small(phys / phyname / "index")
        if index is None or not index.isdecimal():
            raise ValueError("missing Wiphy index")
        control = read_control_port(phyname, int(index))
        checks["custom_control_port_ethertype"] = control["custom_control_port_ethertype"]
        raw = control["control_port_over_nl80211"]
        if raw is not None:
            iw_value = checks["control_port_over_nl80211"]
            if iw_value is not None and iw_value != raw:
                raise ValueError("Wiphy feature mismatch")
            checks["control_port_over_nl80211"] = raw
    except (OSError, ValueError, AttributeError):
        checks["custom_control_port_ethertype"] = None
        report["issues"].append(tr('custom EtherTypeをカーネルへ読取り照会できません。OVER_NL80211だけでは代用しません。'))
    try:
        report["safety"] = _safety(Path(sysfs), phys, phyname, {r["interface"] for r in selected})
        # A hot-unplug/rename must never retain a positive report.
        if _small(phys / phyname / "index") != index or any(
            not (Path(sysfs) / r["interface"] / "phy80211").exists()
            or (Path(sysfs) / r["interface"] / "phy80211").resolve().name != phyname for r in selected
        ):
            raise ValueError("radio identity changed")
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        report["safety"] = {"inspection_complete": False}
        report["issues"].append(tr('接続・経路・rfkill状態を確定できません。安全に使えるとの判定はしません。'))
    if any(value is False for value in checks.values()):
        report["status"] = "requirements_missing"
    elif any(report["safety"].get(key) for key in (
        "default_route_on_phy", "ip_addresses_on_phy", "carrier_on_phy", "rfkill_blocked"
    )):
        report["status"] = "in_use_or_blocked"
        report["issues"].append(tr('選択PHYは接続用/使用中またはrfkillで無効。自動切断・解除しません。別の専用機を用意してください。'))
    elif any(value is None for value in checks.values()) or report["safety"].get("inspection_complete") is False:
        report["status"] = "unknown"
    else:
        report["status"] = "requirements_present_not_verified"
    return report
