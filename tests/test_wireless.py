"""Synthetic-only wireless prerequisite tests. Never scan or operate real radio."""
from pathlib import Path
from types import SimpleNamespace
import json
import socket
import struct
import subprocess
import threading
import time

import pytest

from zasim import wireless as w
from zasim.doctor import check_installation, exit_code

IW = """Wiphy phy7
\tSupported interface modes:
\t\t * managed
\t\t * monitor
\tSupported Ciphers:
\t\t * CCMP (00-0f-ac:4)
\tSupported commands:
\t\t * new_interface
\tBand 1:
\t\tFrequencies:
\t\t\t* 2412 MHz [1] (20.0 dBm)
\t\t\t* 2437 MHz [6] (20.0 dBm)
\t\t\t* 2462 MHz [11] (20.0 dBm)
\tSupported extended features:
\t\t * [ CONTROL_PORT_OVER_NL80211 ]: control port over nl80211
"""


def forbidden(*a, **kw):
    raise AssertionError("no real command, socket, key or hardware operation in test")


@pytest.fixture
def radio(tmp_path, monkeypatch):
    monkeypatch.setattr(w.sys, "platform", "linux")
    net = tmp_path / "net"; net.mkdir()
    phy = tmp_path / "ieee80211/phy7"; phy.mkdir(parents=True)
    (phy / "index").write_text("7\n")
    # Mock symlink identities instead of requiring admin/symlink privilege on Windows.
    interface = net / "wlan7"; interface.mkdir()
    (interface / "phy80211").mkdir()
    (interface / "carrier").write_text("0\n")
    original_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **kw:
                        phy if self.name == "phy80211" else original_resolve(self, *a, **kw))
    rows = [{"interface": "wlan7", "phy": "phy7", "driver": "mt76x0u"}]
    calls = []
    def command(argv):
        calls.append(argv)
        if argv == ["iw", "phy", "phy7", "info"]: return IW
        if argv == ["ip", "-j", "address", "show"]:
            return json.dumps([{"ifname": r["interface"], "addr_info": []} for r in rows])
        if argv in (["ip", "-j", "-4", "route", "show", "table", "all"],
                    ["ip", "-j", "-6", "route", "show", "table", "all"]): return "[]"
        forbidden()
    monkeypatch.setattr(w, "_command", command)
    monkeypatch.setattr(w, "read_control_port", lambda name, index:
                        {"custom_control_port_ethertype": True, "control_port_over_nl80211": True})
    return SimpleNamespace(net=net, phy=phy, rows=rows, calls=calls, command=command,
                           run=lambda **kw: w.inspect_wireless(radios=rows, sysfs=net, **kw))


def test_exact_selected_prerequisites_are_not_wireless_success(radio):
    result = radio.run()
    assert result["status"] == "requirements_present_not_verified"
    assert result["selected_phy"] == "phy7"
    assert all(x is True for x in result["required_checks"].values())
    assert not result["action_frame_io_tested"] and not result["standard_exchange_verified"]
    assert "FRLG" in result["driver_notes"][0]
    assert radio.calls.count(["iw", "phy", "phy7", "info"]) == 1


@pytest.mark.parametrize("replacement,key", [
    ((" * managed", " * AP"), "managed"),
    ((" * monitor", " * P2P-client"), "monitor"),
    (("CCMP (00-0f-ac:4)", "CCMP-256 (00-0f-ac:10)"), "ccmp"),
    (("[ CONTROL_PORT_OVER_NL80211 ]", "[ CONTROL_PORT_OVER_NL80211_TX_STATUS ]"), "control_port_over_nl80211"),
    (("2412 MHz [1] (20.0 dBm)", "2412 MHz [1] (disabled)"), "channel_1"),
    (("2437 MHz [6] (20.0 dBm)", "2437 MHz [6] (20.0 dBm) (no IR)"), "channel_6"),
    (("2462 MHz [11] (20.0 dBm)", "2462 MHz [11] (radar detection)"), "channel_11"),
])
def test_independent_required_flags_fail_closed(radio, monkeypatch, replacement, key):
    monkeypatch.setattr(w, "_command", lambda argv: IW.replace(*replacement) if argv[0] == "iw" else radio.command(argv))
    monkeypatch.setattr(w, "read_control_port", lambda *args:
                        {"custom_control_port_ethertype": True, "control_port_over_nl80211": None})
    result = radio.run()
    assert result["status"] == "requirements_missing"
    assert result["required_checks"][key] is False


@pytest.mark.parametrize("flag", [False, None])
def test_custom_ethertype_is_not_inferred_from_over_nl80211(radio, monkeypatch, flag):
    def read(*args):
        if flag is None: raise PermissionError("secret error details")
        return {"custom_control_port_ethertype": flag, "control_port_over_nl80211": True}
    monkeypatch.setattr(w, "read_control_port", read)
    result = radio.run()
    assert result["status"] == ("unknown" if flag is None else "requirements_missing")
    assert "secret error details" not in json.dumps(result)


def test_mismatched_iw_and_netlink_are_unknown(radio, monkeypatch):
    monkeypatch.setattr(w, "read_control_port", lambda *args:
                        {"custom_control_port_ethertype": True, "control_port_over_nl80211": False})
    assert radio.run()["status"] == "unknown"


@pytest.mark.parametrize("text", ["", IW.replace("phy7", "phy9"), IW + IW.replace("phy7", "phy8")])
def test_mixed_wrong_missing_wiphy_cannot_pass(text):
    with pytest.raises(ValueError): w.parse_iw_phy(text, "phy7")


def test_monitor_only_in_software_combinations_does_not_count():
    text = IW.replace("\t\t * monitor\n", "") + "\tsoftware interface modes (can always be added):\n\t\t * monitor\n"
    assert w.parse_iw_phy(text, "phy7")["monitor"] is False


def test_5ghz_or_no_frequency_output_is_not_24ghz_success():
    text = IW.replace("2412", "5180").replace("2437", "5200").replace("2462", "5220")
    assert all(x is False for x in w.parse_iw_phy(text, "phy7")["social_channels"].values())
    assert w.parse_iw_phy("Wiphy phy7\n", "phy7")["managed"] is None


def test_unselected_radio_cannot_lend_capabilities(radio, monkeypatch):
    (radio.phy.parent / "phy8").mkdir()
    result = radio.run()
    assert result["status"] == "selection_required" and not radio.calls
    result = radio.run(phyname="phy7")
    assert result["status"] == "requirements_present_not_verified"
    assert not any("phy8" in c for c in radio.calls)


@pytest.mark.parametrize("name", ["no-radio", "phy0", "../phy7", "-1", "phy7; true", "phy7\n"])
def test_invalid_or_missing_phy_never_executes(radio, name):
    result = radio.run(phyname=name)
    assert result["status"] in ("not_found", "unknown")
    assert radio.calls == []


def test_no_device_and_windows_are_not_success(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "_command", forbidden)
    monkeypatch.setattr(w, "read_control_port", forbidden)
    monkeypatch.setattr(w.sys, "platform", "linux")
    assert w.inspect_wireless(radios=[], sysfs=tmp_path / "net")["status"] == "not_found"
    monkeypatch.setattr(w.sys, "platform", "win32")
    assert w.inspect_wireless(radios=[], sysfs=tmp_path / "net")["status"] == "linux_backend_required"


@pytest.mark.parametrize("kind", ["ipv4-default", "ipv6-default", "multipath-default", "address", "carrier", "soft", "hard", "sibling-default"])
def test_busy_phy_and_rfkill_are_not_auto_repaired(radio, monkeypatch, kind):
    if kind == "sibling-default":
        radio.rows.append({"interface": "wlan7b", "phy": "phy7", "driver": "mt76x0u"})
        (radio.net / "wlan7b/phy80211").mkdir(parents=True)
        (radio.net / "wlan7b/carrier").write_text("0")
    def command(argv):
        if argv == ["ip", "-j", "-4", "route", "show", "table", "all"]:
            if kind in ("ipv4-default", "sibling-default"):
                return json.dumps([{"dst": "default", "dev": "wlan7b" if kind == "sibling-default" else "wlan7"}])
            if kind == "multipath-default":
                return json.dumps([{"dst": "default", "nexthops": [{"dev": "wlan7"}]}])
        if kind == "ipv6-default" and argv == ["ip", "-j", "-6", "route", "show", "table", "all"]:
            return json.dumps([{"dst": "::/0", "dev": "wlan7"}])
        if kind == "address" and argv == ["ip", "-j", "address", "show"]:
            return json.dumps([{"ifname": "wlan7", "addr_info": [{"local": "private-IP"}]}])
        return radio.command(argv)
    monkeypatch.setattr(w, "_command", command)
    if kind == "carrier": (radio.net / "wlan7/carrier").write_text("1\n")
    if kind in ("soft", "hard"):
        rf = radio.phy / "rfkill3"; rf.mkdir()
        for name in ("soft", "hard"): (rf / name).write_text("1" if name == kind else "0")
    result = radio.run()
    assert result["status"] == "in_use_or_blocked"
    assert "private-IP" not in json.dumps(result)


@pytest.mark.parametrize("problem", ["timeout", "malformed", "disappeared", "renumbered", "rfkill-error"])
def test_inspection_failures_stay_unknown(radio, monkeypatch, problem):
    def command(argv):
        if argv == ["ip", "-j", "address", "show"]:
            if problem == "timeout": raise subprocess.TimeoutExpired(argv, 3)
            if problem == "malformed": return "not JSON"
            if problem == "disappeared": return "[]"
            if problem == "renumbered": (radio.phy / "index").write_text("8")
        return radio.command(argv)
    monkeypatch.setattr(w, "_command", command)
    if problem == "rfkill-error": (radio.phy / "rfkill2").mkdir()
    assert radio.run()["status"] == "unknown"


def test_basic_inventory_missing_driver_is_unknown_not_module(radio):
    assert w.inventory_radios(radio.net)[0]["driver"] == "unknown"


@pytest.mark.parametrize("driver", ["88XXau", "8812au", "rtl8812au", "rtl88xxau", "rtw88_8812au", "rtw_8812au", "mt76x0u", "rtw88_8821ce", "mt7921e", "iwlwifi", "ath9k_htc", "unknown"])
def test_driver_name_alone_never_certifies_za(radio, driver):
    radio.rows[0]["driver"] = driver
    result = radio.run()
    assert result["standard_exchange_verified"] is False
    assert result["driver_notes"]


def nlmsg(kind, seq, body, flags=0):
    data = struct.pack("=IHHII", 16 + len(body), kind, flags, seq, 0) + body
    return data + bytes((-len(data)) % 4)


def genl(kind, seq, attrs):
    return nlmsg(kind, seq, bytes([1 if kind == 16 else 3, 1, 0, 0]) + attrs)


class FakeSocket:
    def __init__(self, packets): self.packets = iter(packets); self.sent = []; self.closed = False
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def bind(self, address): assert address == (0, 0)
    def settimeout(self, seconds): assert 0 < seconds <= 3
    def sendto(self, payload, address): assert address == (0, 0); self.sent.append(payload); return len(payload)
    def recvmsg(self, size):
        value = next(self.packets)
        if isinstance(value, Exception): raise value
        if isinstance(value, tuple): return value
        return value, [], 0, (0, 0)


def make_packets(*, custom=True, ext=b"\0\0\0\4", phy="phy7", index=7):
    family = w._attribute(1, struct.pack("=H", 32)) + w._attribute(2, b"nl80211\0")
    attrs = w._attribute(1, struct.pack("=I", index)) + w._attribute(2, phy.encode() + b"\0")
    if custom: attrs += w._attribute(102, b"")
    if ext is not None: attrs += w._attribute(217, ext)
    ack = lambda seq: nlmsg(2, seq, struct.pack("=i", 0))
    return [genl(16, 1, family) + ack(1), genl(32, 2, attrs) + ack(2)]


@pytest.mark.parametrize("custom,ext,expected", [(True, b"\0\0\0\4", True), (False, b"\0\0\0\4", True), (True, b"\0\0\0\0", False), (True, b"", False), (True, None, None)])
def test_netlink_only_gets_separate_actual_flags(monkeypatch, custom, ext, expected):
    fake = FakeSocket(make_packets(custom=custom, ext=ext))
    families = []
    def create(*args): families.append(args); return fake
    monkeypatch.setattr(w.socket, "socket", create)
    # Windows may not define Linux socket constants; wire layout is platform-independent.
    monkeypatch.setattr(w.socket, "AF_NETLINK", 16, raising=False)
    monkeypatch.setattr(w.socket, "MSG_TRUNC", 32, raising=False)
    result = w.read_control_port("phy7", 7)
    assert result == {"custom_control_port_ethertype": custom, "control_port_over_nl80211": expected}
    assert fake.closed and len(fake.sent) == 2 and families == [(16, socket.SOCK_RAW, 16)]
    first, second = fake.sent
    assert struct.unpack_from("=IHHII", first)[1:4] == (16, 5, 1)
    assert first[16] == 3 and w._attributes(first[20:]) == {2: b"nl80211\0"}
    assert struct.unpack_from("=IHHII", second)[1:4] == (32, 5, 2)
    assert second[16] == 1 and w._attributes(second[20:]) == {1: struct.pack("=I", 7)}


@pytest.mark.parametrize("fault", ["wrong-phy", "wrong-index", "kernel-error", "nonkernel", "truncated", "wrong-seq", "interrupt", "ack-only", "timeout", "wrong-type"])
def test_netlink_errors_never_become_false_success(monkeypatch, fault):
    packets = make_packets(phy="phy8" if fault == "wrong-phy" else "phy7", index=8 if fault == "wrong-index" else 7)
    if fault == "kernel-error": packets[1] = nlmsg(2, 2, struct.pack("=i", -1))
    if fault == "nonkernel": packets[1] = packets[1], [], 0, (9, 0)
    if fault == "truncated": packets[1] = packets[1], [], 32, (0, 0)
    if fault == "wrong-seq": packets[1] = nlmsg(2, 3, struct.pack("=i", 0))
    if fault == "interrupt": packets[1] = nlmsg(2, 2, struct.pack("=i", 0), flags=0x10)
    if fault == "ack-only": packets[1] = nlmsg(2, 2, struct.pack("=i", 0))
    if fault == "timeout": packets[1] = TimeoutError()
    if fault == "wrong-type": packets[1] = nlmsg(99, 2, b"wrong")
    fake = FakeSocket(packets)
    monkeypatch.setattr(w.socket, "socket", lambda *a: fake)
    monkeypatch.setattr(w.socket, "AF_NETLINK", 16, raising=False)
    monkeypatch.setattr(w.socket, "MSG_TRUNC", 32, raising=False)
    with pytest.raises((ValueError, OSError)): w.read_control_port("phy7", 7)
    assert fake.closed


@pytest.mark.parametrize("raw", [b"\x01", struct.pack("=HH", 3, 1), struct.pack("=HH", 100, 1), w._attribute(1, b"one") + w._attribute(1, b"two")])
def test_malformed_or_duplicate_attributes_are_rejected(raw):
    with pytest.raises(ValueError): w._attributes(raw)


@pytest.mark.parametrize("command", [["iw", "dev", "wlan0", "scan"], ["iw", "phy", "phy7;id", "info"], ["ip", "link", "set", "wlan0", "down"], ["sudo", "true"], ["rfkill", "unblock", "all"]])
def test_command_runner_rejects_mutations_and_injections(monkeypatch, command):
    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(ValueError): w._command(command)


def test_command_runner_is_bounded_and_locale_stable(monkeypatch):
    calls = []
    def run(argv, **kwargs): calls.append((argv, kwargs)); return SimpleNamespace(returncode=0, stdout=IW)
    monkeypatch.setattr(subprocess, "run", run)
    assert w._command(["iw", "phy", "phy7", "info"]) == IW
    kwargs = calls[0][1]
    assert kwargs["timeout"] == 3 and kwargs["env"]["LC_ALL"] == "C"
    assert not kwargs.get("shell") and kwargs["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize("status,expected", [("requirements_present_not_verified", 0), ("requirements_missing", 2), ("in_use_or_blocked", 2), ("not_found", 2), ("unknown", 3), ("selection_required", 3), ("linux_backend_required", 2)])
def test_diagnostic_exit_status_does_not_depend_on_tk(status, expected):
    report = {"wireless": {"status": status}, "gui_ready": False, "live_runtime_ready": True}
    assert exit_code(report) == expected
    if expected == 0:
        report["live_runtime_ready"] = False
        assert exit_code(report) == 2


def test_cli_selected_phy_and_json_are_forwarded(monkeypatch, capsys):
    from zatrade import main
    calls = []
    def check(**kw):
        calls.append(kw)
        return {"wireless": {"status": "unknown"}, "gui_ready": True, "live_runtime_ready": True, "keys_read": False}
    monkeypatch.setattr("zasim.doctor.check_installation", check)
    assert main(["doctor", "--backend", "direct", "--radio", "--phy", "phy7", "--json"]) == 3
    assert calls == [{"backend": "direct", "radio": True, "phyname": "phy7"}]
    assert json.loads(capsys.readouterr().out)["wireless"]["status"] == "unknown"


@pytest.mark.parametrize("scale,size", [(1.3333, "800x600"), (2.6667, "640x480")])
def test_gui_readonly_report_mapped_scrollable_and_nonblocking(monkeypatch, tmp_path, scale, size):
    from test_gui_layout import make_app, settle, visible, cleanup
    root, app, errors, infos, callbacks = make_app(monkeypatch, scale)
    called = []; release = threading.Event()
    basic = check_installation(sysfs=tmp_path)
    def check(**kw):
        called.append(kw)
        if kw.get("radio"):
            assert release.wait(3), "GUI main thread blocked during read-only worker"
            result = dict(basic)
            result["wireless"] = {"status": "requirements_missing", "selected_phy": "phy7", "required_checks": {}, "issues": ["synthetic driver detail " * 500], "driver_notes": []}
            return result
        return basic
    monkeypatch.setattr("zasim.doctor.check_installation", check)
    try:
        root.geometry(size); settle(root)
        app.check_button.invoke(); settle(root)
        window = app.environment_window
        visible(app.environment_close_button, window)
        visible(app.environment_radio_button, window)
        assert window.winfo_width() <= root.winfo_width()
        app.environment_phy.set("phy7")
        app.environment_radio_button.invoke(); settle(root)
        assert app._radio_diagnostic_thread.is_alive()
        assert "disabled" in app.environment_radio_button.state()
        release.set(); app._radio_diagnostic_thread.join(2)
        deadline = time.monotonic() + 2
        while "disabled" in app.environment_radio_button.state() and time.monotonic() < deadline:
            root.update(); time.sleep(.01)
        settle(root)
        assert "synthetic driver detail" in app.environment_text.get("1.0", "end")
        assert app.environment_text.yview()[1] < 1
        app.environment_text.yview_moveto(1)
        visible(app.environment_close_button, window)
        assert called[-1]["phyname"] == "phy7" and called[-1]["radio"] is True
        app.environment_close_button.invoke(); settle(root)
        assert not errors and not callbacks
    finally:
        release.set()
        if getattr(app, "_radio_diagnostic_thread", None): app._radio_diagnostic_thread.join(2)
        cleanup(root)


def test_gui_can_close_during_diagnostic_without_tk_worker_calls(monkeypatch, tmp_path):
    from test_gui_layout import make_app, settle, cleanup
    root, app, _, _, callbacks = make_app(monkeypatch)
    release = threading.Event(); basic = check_installation(sysfs=tmp_path)
    def check(**kw):
        if kw.get("radio"): assert release.wait(3)
        return basic
    monkeypatch.setattr("zasim.doctor.check_installation", check)
    try:
        settle(root); app.check_button.invoke(); settle(root)
        app.environment_radio_button.invoke()
        app.environment_close_button.invoke(); settle(root)
        release.set(); app._radio_diagnostic_thread.join(2); settle(root)
        assert not callbacks
    finally:
        release.set(); cleanup(root)


@pytest.mark.parametrize("flags,expected", [(None, "unknown"), ("bad", "unknown"), ("0x1003", "unknown"), ("0x1002", "requirements_present_not_verified")])
def test_unreadable_carrier_requires_proven_admin_down(radio, flags, expected):
    (radio.net / "wlan7/carrier").unlink()
    if flags is not None: (radio.net / "wlan7/flags").write_text(flags)
    assert radio.run()["status"] == expected


def test_generic_netlink_family_cannot_redirect_wiphy_get_to_control(monkeypatch):
    packets = make_packets()
    # A reserved family would change the meaning of command 1. Never send it.
    attrs = w._attribute(1, struct.pack("=H", 16)) + w._attribute(2, b"nl80211\0")
    packets[0] = genl(16, 1, attrs) + nlmsg(2, 1, struct.pack("=i", 0))
    fake = FakeSocket(packets)
    monkeypatch.setattr(w.socket, "socket", lambda *a: fake)
    monkeypatch.setattr(w.socket, "AF_NETLINK", 16, raising=False)
    monkeypatch.setattr(w.socket, "MSG_TRUNC", 32, raising=False)
    with pytest.raises(ValueError, match="family ID"): w.read_control_port("phy7", 7)
    assert len(fake.sent) == 1 and fake.closed


@pytest.mark.parametrize("cipher", ["CCMP", "CCMP-128"])
@pytest.mark.parametrize("decimal", ["", ".0", ".000"])
def test_iw_old_and_current_cipher_and_mhz_formats(cipher, decimal):
    # Real modern iw reports CCMP-128 and 2412.0, unlike older iw. Synthetic
    # identities only; this fixture is not a received RF frame or device proof.
    text = IW.replace("CCMP (", cipher + " (")
    for mhz in ("2412", "2437", "2462"):
        text = text.replace(mhz + " MHz", mhz + decimal + " MHz")
    result = w.parse_iw_phy(text, "phy7")
    assert result["ccmp"] is True
    assert all(result["social_channels"].values())


def test_nearby_fractional_frequency_is_not_expected_channel():
    text = IW.replace("2412 MHz", "2412.5 MHz")
    assert w.parse_iw_phy(text, "phy7")["social_channels"]["1"] is False
