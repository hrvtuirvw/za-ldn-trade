from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import sys
from types import SimpleNamespace

import pytest

if not sys.platform.startswith("linux") or sys.version_info < (3, 12):
    pytest.skip("Linux live-backend regressions require Python 3.12+", allow_module_level=True)
ldn = pytest.importorskip("ldn", reason="install the Linux .[live,test] dependencies")

from zasim.ldn import ldn_single
import zasim.ldn.transport as transport


def _install_fake_ldn_runtime(monkeypatch) -> SimpleNamespace:
    class CancelScope:
        def cancel(self) -> None:
            pass

    class Nursery:
        def __init__(self) -> None:
            self.cancel_scope = CancelScope()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def start_soon(self, _function) -> None:
            # Connection-failure tests finish before the stop watcher is
            # needed.  Ignoring it keeps this asyncio-backed Trio stub small.
            pass

    fake_trio = SimpleNamespace(
        run=lambda function: asyncio.run(function()),
        open_nursery=Nursery,
        sleep=asyncio.sleep,
    )
    fake_ldn = SimpleNamespace(
        ACCEPT_NONE=0,
        ConnectNetworkParam=SimpleNamespace,
        load_keys=lambda _path: {},
    )
    monkeypatch.setitem(sys.modules, "trio", fake_trio)
    monkeypatch.setitem(sys.modules, "ldn", fake_ldn)
    return fake_ldn


def test_interface_state_uses_real_networkmanager_managed_field(monkeypatch) -> None:
    commands = []

    def output(command, **_kwargs):
        commands.append(command)
        if command[0] == "nmcli":
            assert command[2] == "GENERAL.NM-MANAGED"
            return "yes\n"
        return "7: wlan0: <BROADCAST,MULTICAST,UP> mtu 1500\n"

    monkeypatch.setattr(transport.subprocess, "check_output", output)
    assert transport._interface_state("wlan0") == (True, True)
    assert len(commands) == 2


def test_default_route_detection_includes_ipv6(monkeypatch) -> None:
    commands = []

    def output(command, **_kwargs):
        commands.append(command)
        return "default via fe80::1\n" if command[1] == "-6" else ""

    monkeypatch.setattr(transport.subprocess, "check_output", output)

    assert transport.interface_has_default_route("wlan0") is True
    assert [command[1] for command in commands] == ["-4", "-6"]


def test_default_route_detection_fails_closed_if_either_family_cannot_be_read(
    monkeypatch,
) -> None:
    def output(command, **_kwargs):
        if command[1] == "-6":
            raise OSError("IPv6 route inspection failed")
        return "default via 192.0.2.1\n"

    monkeypatch.setattr(transport.subprocess, "check_output", output)

    assert transport.interface_has_default_route("wlan0") is None


def test_auto_reuses_only_netdev_when_phy_lacks_new_interface(monkeypatch) -> None:
    monkeypatch.setattr(
        transport, "list_phy_ifaces", lambda: {"phy0": ["wlx001122334455"]}
    )
    monkeypatch.setattr(transport, "interface_driver", lambda _iface: None)
    monkeypatch.setattr(transport, "phy_supports_new_interface", lambda _phy: False)

    assert transport.choose_single_interface(
        "phy0", "ldnclient", None, log=lambda _message: None
    ) == "wlx001122334455"


def test_auto_mode_fails_closed_when_phy_cannot_create_vif_and_has_no_netdev(
    monkeypatch,
) -> None:
    monkeypatch.setattr(transport, "list_phy_ifaces", lambda: {"phy0": []})
    monkeypatch.setattr(transport, "phy_supports_new_interface", lambda _phy: False)

    with pytest.raises(RuntimeError, match="exactly one non-LDN netdev"):
        transport.choose_single_interface(
            "phy0", "ldnclient", None, log=lambda _message: None
        )


def test_auto_mode_fails_closed_when_iw_interface_inspection_fails(
    monkeypatch,
) -> None:
    monkeypatch.setattr(transport, "list_phy_ifaces", lambda: None)

    with pytest.raises(RuntimeError, match="could not inspect wireless interfaces"):
        transport.choose_single_interface(
            "phy0", "ldnclient", None, log=lambda _message: None
        )


def test_auto_mode_fails_closed_when_vif_support_is_unknown(monkeypatch) -> None:
    monkeypatch.setattr(transport, "list_phy_ifaces", lambda: {"phy0": []})
    monkeypatch.setattr(transport, "phy_supports_new_interface", lambda _phy: None)

    with pytest.raises(RuntimeError, match="could not determine.*virtual interfaces"):
        transport.choose_single_interface(
            "phy0", "ldnclient", None, log=lambda _message: None
        )



def test_stale_phy_resolution_follows_explicit_interface_only_when_old_phy_is_gone(
    monkeypatch,
) -> None:
    logs = []
    monkeypatch.setattr(
        transport, "list_phy_ifaces", lambda: {"phy14": ["wlan0"]}
    )

    assert transport._resolve_reenumerated_single_interface_phy(
        "phy13", "wlan0", logs.append
    ) == "phy14"
    assert logs == [
        "[live] wlan0 was re-enumerated from stale phy13 to phy14; using the current PHY"
    ]


def test_stale_phy_resolution_rejects_inconsistent_live_phy(monkeypatch) -> None:
    monkeypatch.setattr(
        transport,
        "list_phy_ifaces",
        lambda: {"phy13": ["wlan1"], "phy14": ["wlan0"]},
    )

    with pytest.raises(RuntimeError, match="belongs to phy14, not requested phy13"):
        transport._resolve_reenumerated_single_interface_phy(
            "phy13", "wlan0", log=lambda _message: None
        )



def test_single_interface_prepare_follows_reenumerated_explicit_phy(
    monkeypatch,
) -> None:
    live = transport.LiveTransport(
        phyname="phy13",
        ifname="wlan0",
        single_interface=True,
        log=lambda _message: None,
    )
    choices = []

    def choose(phyname, requested_ifname, policy, _log):
        choices.append((phyname, requested_ifname, policy))
        if phyname == "phy13":
            raise RuntimeError("no existing physical interface found")
        assert phyname == "phy14"
        return "wlan0"

    monkeypatch.setattr(transport, "choose_single_interface", choose)
    monkeypatch.setattr(
        transport,
        "_resolve_reenumerated_single_interface_phy",
        lambda _phy, _iface, _log: "phy14",
    )
    monkeypatch.setattr(transport, "interface_has_default_route", lambda _iface: False)
    monkeypatch.setattr(transport, "interface_carries_ssh", lambda _iface: False)
    monkeypatch.setattr(transport, "interface_has_ipv6_state", lambda _iface: False)
    monkeypatch.setattr(transport, "_interface_state", lambda _iface: (True, True))
    monkeypatch.setattr(transport, "wireless_interface_type", lambda _iface: "managed")
    monkeypatch.setattr(
        transport, "interface_mac_address", lambda _iface: "02:11:22:33:44:55"
    )
    monkeypatch.setattr(transport, "interface_ipv4_addresses", lambda _iface: set())
    monkeypatch.setattr(transport, "interface_neighbor_ips", lambda _iface: set())
    monkeypatch.setattr(
        transport, "interface_local_broadcast_routes", lambda _iface: set()
    )
    monkeypatch.setattr(transport, "interface_active_connection", lambda _iface: False)
    monkeypatch.setattr(transport, "interface_autoconnect", lambda _iface: True)
    monkeypatch.setattr(transport, "_read_sysctl", lambda _key: "0")

    live._prepare_radio_path()

    assert live.phyname == "phy14"
    assert live._session_ifname == "wlan0"
    assert choices == [
        ("phy13", "wlan0", True),
        ("phy14", "wlan0", True),
    ]


def test_forced_reuse_rejects_phy_sibling_before_radio_mutation(monkeypatch) -> None:
    live = transport.LiveTransport(
        phyname="phy0",
        ifname="wlan0",
        single_interface=True,
        log=lambda _message: None,
    )
    monkeypatch.setattr(
        transport, "list_phy_ifaces", lambda: {"phy0": ["wlan0", "wlan1"]}
    )
    route_checks = []
    monkeypatch.setattr(
        transport,
        "interface_has_default_route",
        lambda iface: route_checks.append(iface) or (iface == "wlan1"),
    )
    free_calls = []
    monkeypatch.setattr(
        transport,
        "free_radio",
        lambda *_args, **_kwargs: free_calls.append(True),
    )

    with pytest.raises(RuntimeError, match="exactly one non-LDN netdev"):
        live.start()

    assert route_checks == []
    assert free_calls == []


def test_single_interface_prepare_refuses_default_route(monkeypatch) -> None:
    live = transport.LiveTransport(single_interface=True, log=lambda _message: None)
    monkeypatch.setattr(
        transport, "choose_single_interface", lambda *_args: "wlan0"
    )
    monkeypatch.setattr(transport, "interface_has_default_route", lambda _iface: True)

    with pytest.raises(RuntimeError, match="default route"):
        live._prepare_radio_path()


@pytest.mark.parametrize("failed_check", ["route", "ssh"])
def test_single_interface_prepare_fails_closed_on_route_inspection(
    monkeypatch, failed_check
) -> None:
    live = transport.LiveTransport(single_interface=True, log=lambda _message: None)
    monkeypatch.setattr(
        transport, "choose_single_interface", lambda *_args: "wlan0"
    )
    monkeypatch.setattr(
        transport,
        "interface_has_default_route",
        lambda _iface: None if failed_check == "route" else False,
    )
    monkeypatch.setattr(
        transport,
        "interface_carries_ssh",
        lambda _iface: None if failed_check == "ssh" else False,
    )

    with pytest.raises(RuntimeError, match="could not determine"):
        live._prepare_radio_path()
    assert live._single_interface_active is False


@pytest.mark.parametrize(
    ("ipv6_state", "message"),
    [
        (True, "existing IPv6"),
        (None, "could not inspect existing IPv6"),
    ],
)
def test_single_interface_prepare_refuses_unrestorable_ipv6_state(
    monkeypatch, ipv6_state, message
) -> None:
    live = transport.LiveTransport(single_interface=True, log=lambda _message: None)
    monkeypatch.setattr(
        transport, "choose_single_interface", lambda *_args: "wlan0"
    )
    monkeypatch.setattr(transport, "interface_has_default_route", lambda _iface: False)
    monkeypatch.setattr(transport, "interface_carries_ssh", lambda _iface: False)
    monkeypatch.setattr(
        transport, "interface_has_ipv6_state", lambda _iface: ipv6_state
    )

    with pytest.raises(RuntimeError, match=message):
        live._prepare_radio_path()

    assert live._single_interface_active is False


def test_ipv6_state_detection_checks_addresses_routes_and_neighbors(
    monkeypatch,
) -> None:
    commands = []

    def output(command, **_kwargs):
        commands.append(command)
        return "fe80::1 dev wlan0 lladdr 00:11:22:33:44:55\n" if "neigh" in command else ""

    monkeypatch.setattr(transport.subprocess, "check_output", output)

    assert transport.interface_has_ipv6_state("wlan0") is True
    assert [command[2] for command in commands] == ["-o", "route", "neigh"]


def test_single_interface_prepare_snapshots_all_mutable_state(monkeypatch) -> None:
    live = transport.LiveTransport(single_interface=True, log=lambda _message: None)
    monkeypatch.setattr(
        transport, "choose_single_interface", lambda *_args: "wlan0"
    )
    monkeypatch.setattr(transport, "interface_has_default_route", lambda _iface: False)
    monkeypatch.setattr(transport, "interface_carries_ssh", lambda _iface: False)
    monkeypatch.setattr(transport, "interface_has_ipv6_state", lambda _iface: False)
    monkeypatch.setattr(transport, "_interface_state", lambda _iface: (True, True))
    monkeypatch.setattr(transport, "wireless_interface_type", lambda _iface: "managed")
    monkeypatch.setattr(
        transport,
        "interface_mac_address",
        lambda _iface: "02:11:22:33:44:55",
    )
    monkeypatch.setattr(
        transport, "interface_ipv4_addresses", lambda _iface: {"192.0.2.2/24"}
    )
    monkeypatch.setattr(
        transport, "interface_neighbor_ips", lambda _iface: {"192.0.2.1"}
    )
    monkeypatch.setattr(
        transport, "interface_local_broadcast_routes", lambda _iface: {"192.0.2.255"}
    )
    monkeypatch.setattr(
        transport, "interface_active_connection", lambda _iface: "profile-uuid"
    )
    monkeypatch.setattr(transport, "interface_autoconnect", lambda _iface: True)
    monkeypatch.setattr(transport, "_read_sysctl", lambda _key: "0")

    live._prepare_radio_path()

    assert live._session_ifname == "wlan0"
    assert live._wireless_type_restore_state == {"wlan0": "managed"}
    assert live._single_interface_original_addresses == {"192.0.2.2/24"}
    assert live._single_interface_original_neighbors == {"192.0.2.1"}
    assert live._single_interface_original_broadcast_routes == {"192.0.2.255"}
    assert live._single_interface_original_mac == "02:11:22:33:44:55"
    assert live._connection_restore_state == {"wlan0": "profile-uuid"}
    assert live._connection_baseline_state == {"wlan0": "profile-uuid"}
    assert live._autoconnect_baseline_state == {"wlan0": True}
    assert live._sysctl_restore_state == {
        "net.ipv6.conf.wlan0.disable_ipv6": "0"
    }


def test_usb_wireless_identity_binds_sysfs_and_device_node_numbers(
    monkeypatch,
) -> None:
    interface_path = "/sys/devices/pci/usb1/1-8/1-8:1.0"
    device_path = "/sys/devices/pci/usb1/1-8"
    values = {
        f"{device_path}/idVendor": "0bda",
        f"{device_path}/idProduct": "8812",
        f"{device_path}/busnum": "1",
        f"{device_path}/devnum": "14",
        f"{device_path}/dev": "189:13",
        f"{device_path}/serial": "123456",
    }

    def realpath(path):
        if path == "/sys/class/net/wlan0/device":
            return interface_path
        if path == f"{interface_path}/driver":
            return "/sys/bus/usb/drivers/rtw88_8812au"
        return path

    monkeypatch.setattr(transport.os.path, "realpath", realpath)
    monkeypatch.setattr(
        transport.os.path, "isdir", lambda path: path == interface_path
    )
    monkeypatch.setattr(
        transport, "_read_sysfs_value", lambda path: values.get(path)
    )
    monkeypatch.setattr(
        transport.os,
        "listdir",
        lambda path: ["wlan0"]
        if path == f"{interface_path}/net"
        else pytest.fail("unexpected directory"),
    )

    assert transport._usb_wireless_identity("wlan0") == {
        "iface": "wlan0",
        "interface_path": interface_path,
        "device_path": device_path,
        "port": "1-8",
        "vendor": "0bda",
        "product": "8812",
        "serial": "123456",
        "driver": "rtw88_8812au",
        "busnum": 1,
        "devnum": 14,
        "device_major": 189,
        "device_minor": 13,
    }


def test_restore_interface_mac_address_is_verified(monkeypatch) -> None:
    commands = []
    values = iter(("02:aa:bb:cc:dd:ee", "02:11:22:33:44:55"))
    monkeypatch.setattr(
        transport, "interface_mac_address", lambda _iface: next(values)
    )
    monkeypatch.setattr(
        transport,
        "_run",
        lambda command: commands.append(command) or SimpleNamespace(returncode=0),
    )

    transport.restore_interface_mac_address(
        "wlan0", "02:11:22:33:44:55", log=lambda _message: None
    )

    assert commands == [
        ["nmcli", "device", "set", "wlan0", "managed", "no"],
        ["ip", "link", "set", "wlan0", "down"],
        [
            "ip", "link", "set", "wlan0", "address",
            "02:11:22:33:44:55",
        ],
    ]


def test_reset_silent_usb_radio_revalidates_exact_device_and_returns_new_phy(
    monkeypatch,
) -> None:
    old = {
        "iface": "wlan0",
        "interface_path": "/sys/devices/usb1/1-8/1-8:1.0",
        "device_path": "/sys/devices/usb1/1-8",
        "port": "1-8",
        "vendor": "0bda",
        "product": "8812",
        "serial": "123456",
        "driver": "rtw88_8812au",
        "busnum": 1,
        "devnum": 14,
        "device_major": 189,
        "device_minor": 13,
    }
    returned = dict(old, devnum=15)
    identities = iter((old, old, old, returned, returned))
    commands = []
    opened = []
    closed = []
    ioctls = []
    monkeypatch.setattr(
        transport, "_usb_wireless_identity", lambda _iface: next(identities)
    )
    monkeypatch.setattr(transport, "interface_active_connection", lambda _iface: False)
    monkeypatch.setattr(transport, "interface_has_default_route", lambda _iface: False)
    monkeypatch.setattr(transport, "interface_carries_ssh", lambda _iface: False)
    monkeypatch.setattr(transport, "interface_has_ipv6_state", lambda _iface: False)
    monkeypatch.setattr(transport, "interface_ipv4_addresses", lambda _iface: set())
    monkeypatch.setattr(transport, "interface_neighbor_ips", lambda _iface: set())
    monkeypatch.setattr(
        transport, "interface_local_broadcast_routes", lambda _iface: set()
    )
    monkeypatch.setattr(transport, "wireless_interface_type", lambda _iface: "managed")
    monkeypatch.setattr(
        transport, "interface_mac_address", lambda _iface: "02:11:22:33:44:55"
    )
    monkeypatch.setattr(
        transport, "list_phy_ifaces", lambda: {"phy14": ["wlan0"]}
    )
    monkeypatch.setattr(
        transport,
        "_run",
        lambda command: commands.append(command) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(transport.time, "sleep", lambda _seconds: None)

    def open_device(path, flags):
        opened.append((path, flags))
        return 91

    monkeypatch.setattr(transport.os, "open", open_device)
    monkeypatch.setattr(
        transport.os,
        "fstat",
        lambda descriptor: SimpleNamespace(st_mode=0, st_rdev=0)
        if descriptor == 91
        else pytest.fail("unexpected descriptor"),
    )
    monkeypatch.setattr(transport.stat, "S_ISCHR", lambda _mode: True)
    monkeypatch.setattr(transport.os, "major", lambda _device: 189, raising=False)
    monkeypatch.setattr(transport.os, "minor", lambda _device: 13, raising=False)
    monkeypatch.setattr(transport.os, "close", lambda descriptor: closed.append(descriptor))
    monkeypatch.setitem(
        sys.modules,
        "fcntl",
        SimpleNamespace(
            ioctl=lambda descriptor, request, argument: ioctls.append(
                (descriptor, request, argument)
            )
        ),
    )

    assert transport.reset_silent_usb_radio(
        "wlan0", "02:11:22:33:44:55", log=lambda _message: None
    ) == "phy14"
    assert opened == [(
        "/dev/bus/usb/001/014",
        transport.os.O_WRONLY
        | getattr(transport.os, "O_CLOEXEC", 0)
        | getattr(transport.os, "O_NOFOLLOW", 0),
    )]
    assert ioctls == [(91, transport._USBDEVFS_RESET, 0)]
    assert closed == [91]
    assert commands == [
        ["nmcli", "device", "set", "wlan0", "managed", "no"],
        ["ip", "link", "set", "wlan0", "down"],
        ["nmcli", "device", "set", "wlan0", "managed", "no"],
        ["ip", "link", "set", "wlan0", "down"],
        ["iw", "dev", "wlan0", "set", "type", "managed"],
        [
            "ip", "link", "set", "wlan0", "address",
            "02:11:22:33:44:55",
        ],
    ]


def test_reset_silent_usb_radio_rejects_mismatched_device_node_before_ioctl(
    monkeypatch,
) -> None:
    identity = {
        "iface": "wlan0",
        "interface_path": "/sys/devices/usb1/1-8/1-8:1.0",
        "device_path": "/sys/devices/usb1/1-8",
        "port": "1-8",
        "vendor": "0bda",
        "product": "8812",
        "serial": "123456",
        "driver": "rtw88_8812au",
        "busnum": 1,
        "devnum": 14,
        "device_major": 189,
        "device_minor": 13,
    }
    identities = iter((identity, identity))
    commands = []
    closed = []
    ioctls = []
    monkeypatch.setattr(
        transport, "_usb_wireless_identity", lambda _iface: next(identities)
    )
    monkeypatch.setattr(transport, "interface_active_connection", lambda _iface: False)
    monkeypatch.setattr(transport, "interface_has_default_route", lambda _iface: False)
    monkeypatch.setattr(transport, "interface_carries_ssh", lambda _iface: False)
    monkeypatch.setattr(transport, "interface_has_ipv6_state", lambda _iface: False)
    monkeypatch.setattr(transport, "interface_ipv4_addresses", lambda _iface: set())
    monkeypatch.setattr(transport, "interface_neighbor_ips", lambda _iface: set())
    monkeypatch.setattr(
        transport, "interface_local_broadcast_routes", lambda _iface: set()
    )
    monkeypatch.setattr(transport, "wireless_interface_type", lambda _iface: "managed")
    monkeypatch.setattr(
        transport,
        "_run",
        lambda command: commands.append(command) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(transport.os, "open", lambda _path, _flags: 92)
    monkeypatch.setattr(
        transport.os,
        "fstat",
        lambda _descriptor: SimpleNamespace(st_mode=0, st_rdev=0),
    )
    monkeypatch.setattr(transport.stat, "S_ISCHR", lambda _mode: True)
    monkeypatch.setattr(transport.os, "major", lambda _device: 190, raising=False)
    monkeypatch.setattr(transport.os, "minor", lambda _device: 13, raising=False)
    monkeypatch.setattr(transport.os, "close", lambda descriptor: closed.append(descriptor))
    monkeypatch.setitem(
        sys.modules,
        "fcntl",
        SimpleNamespace(
            ioctl=lambda *args: ioctls.append(args)
        ),
    )

    with pytest.raises(RuntimeError, match="device node does not match"):
        transport.reset_silent_usb_radio(
            "wlan0", "02:11:22:33:44:55", log=lambda _message: None
        )

    assert ioctls == []
    assert closed == [92]
    assert commands == [
        ["nmcli", "device", "set", "wlan0", "managed", "no"],
        ["ip", "link", "set", "wlan0", "down"],
    ]


@pytest.mark.parametrize(
    ("active_connection", "default_route"),
    (("profile-uuid", False), (False, True)),
)
def test_reset_silent_usb_radio_refuses_interface_with_host_state(
    monkeypatch,
    active_connection,
    default_route,
) -> None:
    identity = {
        "iface": "wlan0",
        "interface_path": "/sys/devices/usb1/1-8/1-8:1.0",
        "device_path": "/sys/devices/usb1/1-8",
        "port": "1-8",
        "vendor": "0bda",
        "product": "8812",
        "serial": "123456",
        "driver": "rtw88_8812au",
        "busnum": 1,
        "devnum": 14,
        "device_major": 189,
        "device_minor": 13,
    }
    mutations = []
    monkeypatch.setattr(transport, "_usb_wireless_identity", lambda _iface: identity)
    monkeypatch.setattr(
        transport,
        "interface_active_connection",
        lambda _iface: active_connection,
    )
    monkeypatch.setattr(
        transport,
        "interface_has_default_route",
        lambda _iface: default_route,
    )
    monkeypatch.setattr(transport, "interface_carries_ssh", lambda _iface: False)
    monkeypatch.setattr(transport, "interface_has_ipv6_state", lambda _iface: False)
    monkeypatch.setattr(transport, "interface_ipv4_addresses", lambda _iface: set())
    monkeypatch.setattr(transport, "interface_neighbor_ips", lambda _iface: set())
    monkeypatch.setattr(
        transport, "interface_local_broadcast_routes", lambda _iface: set()
    )
    monkeypatch.setattr(
        transport, "_run", lambda command: mutations.append(command)
    )

    with pytest.raises(RuntimeError, match="carries host network state"):
        transport.reset_silent_usb_radio(
            "wlan0", "02:11:22:33:44:55", log=lambda _message: None
        )
    assert mutations == []


def test_cleanup_failure_still_attempts_every_restoration_step(monkeypatch) -> None:
    events = []
    live = transport.LiveTransport(log=lambda _message: None)
    live._single_interface_active = True
    live._single_interface_session = True

    def fail_network(*, final=False):
        assert final
        events.append("network")
        raise RuntimeError("address cleanup failed")

    monkeypatch.setattr(live, "_cleanup_single_interface_network", fail_network)
    monkeypatch.setattr(
        transport,
        "restore_wireless_interface_types",
        lambda *_args: events.append("type"),
    )
    monkeypatch.setattr(
        transport,
        "restore_network_state",
        lambda *_args: events.append("nm-sysctl"),
    )
    monkeypatch.setattr(
        transport,
        "restore_active_connections",
        lambda *_args: events.append("connection"),
    )
    monkeypatch.setattr(transport, "interface_ipv4_addresses", lambda _iface: set())
    monkeypatch.setattr(transport, "interface_neighbor_ips", lambda _iface: set())
    monkeypatch.setattr(
        transport, "interface_local_broadcast_routes", lambda _iface: set()
    )

    with pytest.raises(RuntimeError, match="address cleanup failed"):
        live._restore_radio_state()
    assert events == ["network", "type", "nm-sysctl", "connection"]


def test_restore_active_connection_only_when_snapshot_contains_one(monkeypatch) -> None:
    commands = []
    monkeypatch.setattr(
        transport,
        "_run",
        lambda command: commands.append(command) or SimpleNamespace(returncode=0),
    )
    disconnected = {}
    transport.restore_active_connections(disconnected, log=lambda _message: None)
    assert commands == []

    connected = {"wlan0": "profile-uuid"}
    transport.restore_active_connections(connected, log=lambda _message: None)
    assert commands == [[
        "nmcli", "connection", "up", "uuid", "profile-uuid", "ifname", "wlan0"
    ]]
    # The caller clears the lease only after querying NM and verifying UUID.
    assert connected == {"wlan0": "profile-uuid"}


@pytest.mark.parametrize(
    ("command_returncode", "readback", "message"),
    [
        (1, "1", "could not restore sysctl"),
        (0, "0", "expected '1' after cleanup"),
    ],
)
def test_restore_network_state_rejects_sysctl_command_or_readback_failure(
    monkeypatch, command_returncode, readback, message
) -> None:
    sysctls = {"net.ipv4.conf.all.rp_filter": "1"}
    monkeypatch.setattr(
        transport,
        "_sysctl",
        lambda *_args: SimpleNamespace(returncode=command_returncode),
    )
    monkeypatch.setattr(transport, "_read_sysctl", lambda _key: readback)

    with pytest.raises(RuntimeError, match=message):
        transport.restore_network_state({}, sysctls, log=lambda _message: None)

    assert sysctls == {"net.ipv4.conf.all.rp_filter": "1"}


def test_restore_network_state_rejects_unknown_persistent_sysctl_baseline() -> None:
    sysctls = {"net.ipv4.conf.all.rp_filter": None}

    with pytest.raises(RuntimeError, match="restoration cannot be verified"):
        transport.restore_network_state({}, sysctls, log=lambda _message: None)

    assert sysctls == {"net.ipv4.conf.all.rp_filter": None}


@pytest.mark.parametrize(
    ("command_fails", "actual_state", "message"),
    [
        ("managed", (True, True), "could not restore NetworkManager"),
        ("link", (True, True), "could not restore administrative link"),
        (None, (False, True), "managed state was not restored"),
        (None, (True, False), "link state was not restored"),
    ],
)
def test_restore_network_state_rejects_interface_command_or_readback_failure(
    monkeypatch, command_fails, actual_state, message
) -> None:
    state = {"wlan0": (True, True)}

    def run(command):
        kind = "managed" if command[0] == "nmcli" else "link"
        return SimpleNamespace(returncode=int(kind == command_fails))

    monkeypatch.setattr(transport, "_run", run)
    monkeypatch.setattr(transport, "_interface_state", lambda _iface: actual_state)

    with pytest.raises(RuntimeError, match=message):
        transport.restore_network_state(state, {}, log=lambda _message: None)

    assert state == {"wlan0": (True, True)}


def test_normal_vif_restore_retains_lease_when_network_command_fails(
    monkeypatch,
) -> None:
    live = transport.LiveTransport(single_interface=False, log=lambda _message: None)
    live._radio_restore_state = {"wlan0": (True, True)}

    def run(command):
        return SimpleNamespace(returncode=int(command[0] == "nmcli"))

    monkeypatch.setattr(transport, "_run", run)
    monkeypatch.setattr(transport, "_interface_state", lambda _iface: (True, True))

    with pytest.raises(RuntimeError, match="radio cleanup incomplete"):
        live._restore_radio_state()

    assert live._radio_restore_state == {"wlan0": (True, True)}


def test_removed_normal_vif_sysctls_do_not_block_strict_global_restore(
    monkeypatch,
) -> None:
    sysctls = {
        "net.ipv4.conf.ldnclient.rp_filter": "1",
        "net.ipv4.conf.all.rp_filter": "1",
    }
    writes = []
    monkeypatch.setattr(
        transport, "_iface_exists", lambda iface: iface != "ldnclient"
    )
    monkeypatch.setattr(
        transport,
        "_sysctl",
        lambda key, value: writes.append((key, value))
        or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(transport, "_read_sysctl", lambda _key: "1")

    transport.restore_network_state({}, sysctls, log=lambda _message: None)

    assert writes == [("net.ipv4.conf.all.rp_filter", "1")]
    assert sysctls == {}


@pytest.mark.parametrize("iface", ["za-custom", "za.custom"])
def test_removed_custom_ephemeral_vif_sysctls_do_not_block_restore(
    monkeypatch, iface
) -> None:
    sysctls = {
        f"net.ipv4.conf.{iface}.rp_filter": "1",
        f"net.ipv4.conf.{iface}.accept_local": "0",
    }
    writes = []
    monkeypatch.setattr(transport, "_iface_exists", lambda _iface: False)
    monkeypatch.setattr(
        transport,
        "_sysctl",
        lambda key, value: writes.append((key, value))
        or SimpleNamespace(returncode=0),
    )

    transport.restore_network_state(
        {},
        sysctls,
        log=lambda _message: None,
        ephemeral_ifaces={iface},
    )

    assert writes == []
    assert sysctls == {}


@pytest.mark.parametrize(
    "key",
    [
        "net.ipv4.conf.all.rp_filter",
        "net.ipv6.conf.default.disable_ipv6",
    ],
)
def test_reserved_ephemeral_names_keep_global_and_default_sysctls_strict(
    monkeypatch, key
) -> None:
    sysctls = {key: "1"}
    monkeypatch.setattr(transport, "_iface_exists", lambda _iface: False)
    monkeypatch.setattr(
        transport,
        "_sysctl",
        lambda *_args: SimpleNamespace(returncode=1),
    )

    with pytest.raises(RuntimeError, match="could not restore sysctl"):
        transport.restore_network_state(
            {},
            sysctls,
            log=lambda _message: None,
            ephemeral_ifaces={"all", "default"},
        )

    assert sysctls == {key: "1"}


def test_tune_iface_snapshots_every_sysctl_before_first_write(monkeypatch) -> None:
    restore_state = {}
    writes = []
    host_commands = []

    def read(key):
        return None if key == "net.ipv4.conf.all.rp_filter" else "1"

    monkeypatch.setattr(transport, "_read_sysctl", read)
    monkeypatch.setattr(
        transport,
        "_sysctl",
        lambda key, value: writes.append((key, value))
        or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        transport,
        "_run",
        lambda command: host_commands.append(command) or SimpleNamespace(returncode=0),
    )

    with pytest.raises(RuntimeError, match="could not snapshot sysctl"):
        transport.tune_iface(
            "wlan0",
            "169.254.7.2",
            "169.254.7.255",
            restore_state=restore_state,
            remove_stray=False,
        )

    assert writes == []
    assert host_commands == []
    assert restore_state == {}


def test_tune_iface_retains_complete_snapshot_on_sysctl_write_failure(
    monkeypatch,
) -> None:
    restore_state = {}
    writes = []
    monkeypatch.setattr(transport, "_read_sysctl", lambda _key: "1")

    def write(key, value):
        writes.append((key, value))
        return SimpleNamespace(returncode=int(key == "net.ipv4.conf.all.rp_filter"))

    monkeypatch.setattr(transport, "_sysctl", write)
    monkeypatch.setattr(
        transport, "_run", lambda _command: SimpleNamespace(returncode=0)
    )

    with pytest.raises(RuntimeError, match="could not set sysctl"):
        transport.tune_iface(
            "wlan0",
            "169.254.7.2",
            "169.254.7.255",
            restore_state=restore_state,
            remove_stray=False,
        )

    assert len(restore_state) == 4
    assert writes[:2] == [
        ("net.ipv4.conf.wlan0.rp_filter", "0"),
        ("net.ipv4.conf.all.rp_filter", "0"),
    ]


def test_tune_iface_surfaces_broadcast_route_failure(monkeypatch) -> None:
    monkeypatch.setattr(transport, "_read_sysctl", lambda _key: "1")
    monkeypatch.setattr(
        transport, "_sysctl", lambda *_args: SimpleNamespace(returncode=0)
    )

    def run(command):
        is_route = command[:3] == ["ip", "route", "replace"]
        return SimpleNamespace(returncode=int(is_route))

    monkeypatch.setattr(transport, "_run", run)

    with pytest.raises(RuntimeError, match="broadcast route"):
        transport.tune_iface(
            "wlan0",
            "169.254.7.2",
            "169.254.7.255",
            restore_state={},
            remove_stray=False,
        )


def test_network_cleanup_removes_only_state_added_after_snapshot(monkeypatch) -> None:
    live = transport.LiveTransport(log=lambda _message: None)
    live._single_interface_active = True
    live._session_ifname = "wlan0"
    live._single_interface_original_addresses = {"192.0.2.2/24"}
    live._single_interface_original_neighbors = {"192.0.2.1"}
    live._single_interface_original_broadcast_routes = {"192.0.2.255"}
    addresses = {"192.0.2.2/24", "169.254.7.2/24"}
    neighbors = {"192.0.2.1", "169.254.7.1"}
    routes = {"192.0.2.255", "169.254.7.255"}

    monkeypatch.setattr(transport, "interface_ipv4_addresses", lambda _iface: set(addresses))
    monkeypatch.setattr(transport, "interface_neighbor_ips", lambda _iface: set(neighbors))
    monkeypatch.setattr(
        transport, "interface_local_broadcast_routes", lambda _iface: set(routes)
    )

    def run(command):
        if command[:4] == ["ip", "addr", "del", "169.254.7.2/24"]:
            addresses.remove("169.254.7.2/24")
        elif command[:4] == ["ip", "neigh", "del", "169.254.7.1"]:
            neighbors.remove("169.254.7.1")
        elif "169.254.7.255" in command:
            routes.remove("169.254.7.255")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(transport, "_run", run)
    live._cleanup_single_interface_network(final=True)

    assert addresses == {"192.0.2.2/24"}
    assert neighbors == {"192.0.2.1"}
    assert routes == {"192.0.2.255"}


def test_light_cleanup_raises_when_known_vif_survives(monkeypatch) -> None:
    deletes = []
    monkeypatch.setattr(transport, "_iw_del", lambda iface: deletes.append(iface))
    monkeypatch.setattr(
        transport,
        "list_phy_ifaces",
        lambda: {"phy0": ["wlan0", "ldnclient"]},
    )
    monkeypatch.setattr(
        transport, "_iface_exists", lambda iface: iface == "ldnclient"
    )
    monkeypatch.setattr(transport.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="ldnclient"):
        transport.light_cleanup(
            log=lambda _message: None,
            target_phys={"phy0"},
            owned_ifaces={"ldnclient"},
        )

    assert deletes == ["ldnclient"]


def test_light_cleanup_deletes_only_owned_vif_on_target_phy(monkeypatch) -> None:
    mapping = {
        "phy0": ["wlan0", "ldnclient"],
        "phy1": ["other-monitor"],
    }
    deletes = []

    def delete(iface):
        deletes.append(iface)
        mapping["phy0"].remove(iface)

    monkeypatch.setattr(transport, "list_phy_ifaces", lambda: mapping)
    monkeypatch.setattr(
        transport,
        "_iface_exists",
        lambda iface: any(iface in ifaces for ifaces in mapping.values()),
    )
    monkeypatch.setattr(transport, "_iw_del", delete)
    monkeypatch.setattr(transport.time, "sleep", lambda _seconds: None)

    transport.light_cleanup(
        target_phys={"phy0"},
        owned_ifaces={"ldnclient"},
        log=lambda _message: None,
    )

    assert deletes == ["ldnclient"]
    assert mapping["phy1"] == ["other-monitor"]


def test_light_cleanup_deletes_exact_owned_custom_vif(monkeypatch) -> None:
    mapping = {"phy0": ["wlan0", "za-custom"]}
    deletes = []

    def delete(iface):
        deletes.append(iface)
        mapping["phy0"].remove(iface)

    monkeypatch.setattr(transport, "list_phy_ifaces", lambda: mapping)
    monkeypatch.setattr(
        transport,
        "_iface_exists",
        lambda iface: any(iface in ifaces for ifaces in mapping.values()),
    )
    monkeypatch.setattr(transport, "_iw_del", delete)
    monkeypatch.setattr(transport.time, "sleep", lambda _seconds: None)

    transport.light_cleanup(
        target_phys={"phy0"},
        owned_ifaces={"za-custom"},
        log=lambda _message: None,
    )

    assert deletes == ["za-custom"]


@pytest.mark.parametrize(
    ("mapping", "custom_exists", "message"),
    [
        (
            {"phy0": ["wlan0", "za-custom"]},
            False,
            "not owned by this LDN session",
        ),
        (
            {"phy0": ["wlan0"]},
            True,
            "PHY ownership is unknown",
        ),
    ],
)
def test_free_radio_rejects_preexisting_custom_requested_vif_before_mutation(
    monkeypatch, mapping, custom_exists, message
) -> None:
    snapshots = []
    deletes = []
    commands = []
    monkeypatch.setattr(transport, "list_phy_ifaces", lambda: mapping)
    monkeypatch.setattr(
        transport,
        "_iface_exists",
        lambda iface: custom_exists and iface == "za-custom",
    )
    monkeypatch.setattr(
        transport,
        "_interface_state",
        lambda iface: snapshots.append(iface) or (True, True),
    )
    monkeypatch.setattr(transport, "_iw_del", lambda iface: deletes.append(iface))
    monkeypatch.setattr(
        transport,
        "_run",
        lambda command: commands.append(command) or SimpleNamespace(returncode=0),
    )

    with pytest.raises(RuntimeError, match=message):
        transport.free_radio(
            {"phy0"},
            restore_state={},
            reserved_ifaces={"za-custom"},
            log=lambda _message: None,
        )

    assert snapshots == []
    assert deletes == []
    assert commands == []


def test_free_radio_deletes_only_current_session_owned_custom_vif(
    monkeypatch,
) -> None:
    mapping = {"phy0": ["wlan0", "za-custom"]}
    deletes = []
    commands = []

    def delete(iface):
        deletes.append(iface)
        mapping["phy0"].remove(iface)

    monkeypatch.setattr(transport, "list_phy_ifaces", lambda: mapping)
    monkeypatch.setattr(
        transport,
        "_iface_exists",
        lambda iface: any(iface in ifaces for ifaces in mapping.values()),
    )
    monkeypatch.setattr(transport, "_iw_del", delete)
    monkeypatch.setattr(
        transport,
        "_run",
        lambda command: commands.append(command) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(transport.time, "sleep", lambda _seconds: None)
    restore_state = {"wlan0": (True, True)}

    transport.free_radio(
        {"phy0"},
        restore_state=restore_state,
        reserved_ifaces={"za-custom"},
        owned_ifaces={"za-custom"},
        log=lambda _message: None,
    )

    assert deletes == ["za-custom"]
    assert restore_state == {"wlan0": (True, True)}
    assert all(
        "za-custom" not in command
        for command in commands
        if command[0] == "nmcli"
    )


@pytest.mark.parametrize("operation", ["free", "light"])
@pytest.mark.parametrize(
    ("ownership", "mapping", "exists", "message"),
    [
        (
            "off-target",
            {"phy0": ["wlan0"], "phy1": ["ldnclient"]},
            False,
            "off-target phy1",
        ),
        (
            "unknown",
            {"phy0": ["wlan0"]},
            True,
            "PHY ownership is unknown",
        ),
    ],
)
def test_vif_cleanup_scope_fails_before_side_effects(
    monkeypatch, operation, ownership, mapping, exists, message
) -> None:
    del ownership  # the id keeps parametrized failures readable
    deletes = []
    commands = []
    monkeypatch.setattr(transport, "list_phy_ifaces", lambda: mapping)
    monkeypatch.setattr(
        transport,
        "_iface_exists",
        lambda iface: exists and iface == "ldnclient",
    )
    monkeypatch.setattr(transport, "_iw_del", lambda iface: deletes.append(iface))
    monkeypatch.setattr(
        transport,
        "_run",
        lambda command: commands.append(command) or SimpleNamespace(returncode=0),
    )

    with pytest.raises(RuntimeError, match=message):
        if operation == "free":
            transport.free_radio(
                {"phy0"},
                restore_state={},
                log=lambda _message: None,
            )
        else:
            transport.light_cleanup(
                target_phys={"phy0"},
                owned_ifaces=transport.LDN_VIFS,
                log=lambda _message: None,
            )

    assert deletes == []
    assert commands == []


def test_light_cleanup_refuses_target_vif_without_session_ownership(
    monkeypatch,
) -> None:
    deletes = []
    monkeypatch.setattr(
        transport,
        "list_phy_ifaces",
        lambda: {"phy0": ["wlan0", "ldnclient"]},
    )
    monkeypatch.setattr(transport, "_iface_exists", lambda _iface: False)
    monkeypatch.setattr(transport, "_iw_del", lambda iface: deletes.append(iface))

    with pytest.raises(RuntimeError, match="not owned by this LDN session"):
        transport.light_cleanup(
            target_phys={"phy0"},
            owned_ifaces=set(),
            log=lambda _message: None,
        )

    assert deletes == []


def test_free_radio_reuse_path_never_issues_vif_delete(monkeypatch) -> None:
    deletes = []
    monkeypatch.setattr(
        transport, "list_phy_ifaces", lambda: {"phy0": ["wlan0"]}
    )
    monkeypatch.setattr(transport, "_iface_exists", lambda _iface: False)
    monkeypatch.setattr(transport, "_interface_state", lambda _iface: (True, True))
    monkeypatch.setattr(transport, "_iw_del", lambda iface: deletes.append(iface))
    monkeypatch.setattr(
        transport, "_run", lambda _command: SimpleNamespace(returncode=0)
    )
    monkeypatch.setattr(transport.time, "sleep", lambda _seconds: None)
    restore_state = {"wlan0": (True, True)}

    transport.free_radio(
        {"phy0"},
        log=lambda _message: None,
        restore_state=restore_state,
        delete_vifs=False,
    )

    assert deletes == []
    assert restore_state == {"wlan0": (True, True)}


def test_free_radio_reuse_rejects_sibling_added_after_preflight(monkeypatch) -> None:
    commands = []
    deletes = []
    monkeypatch.setattr(
        transport, "list_phy_ifaces", lambda: {"phy0": ["wlan0", "wlan1"]}
    )
    monkeypatch.setattr(transport, "_iface_exists", lambda _iface: False)
    monkeypatch.setattr(transport, "_iw_del", lambda iface: deletes.append(iface))
    monkeypatch.setattr(
        transport,
        "_run",
        lambda command: commands.append(command) or SimpleNamespace(returncode=0),
    )

    with pytest.raises(RuntimeError, match="membership changed after preflight"):
        transport.free_radio(
            {"phy0"},
            log=lambda _message: None,
            restore_state={"wlan0": (True, True)},
            delete_vifs=False,
        )

    assert commands == []
    assert deletes == []


def test_free_radio_reuse_rejects_stale_vif_before_any_mutation(monkeypatch) -> None:
    commands = []
    deletes = []
    monkeypatch.setattr(
        transport,
        "list_phy_ifaces",
        lambda: {"phy0": ["wlan0", "ldnclient"]},
    )
    monkeypatch.setattr(transport, "_iface_exists", lambda _iface: False)
    monkeypatch.setattr(transport, "_iw_del", lambda iface: deletes.append(iface))
    monkeypatch.setattr(
        transport,
        "_run",
        lambda command: commands.append(command) or SimpleNamespace(returncode=0),
    )

    with pytest.raises(RuntimeError, match="already exist"):
        transport.free_radio(
            {"phy0"},
            log=lambda _message: None,
            restore_state={},
            delete_vifs=False,
        )

    assert commands == []
    assert deletes == []


def test_single_interface_teardown_verifies_without_deleting_vifs(
    monkeypatch,
) -> None:
    live = transport.LiveTransport(log=lambda _message: None)
    live._single_interface_active = True
    live._single_interface_session = True
    deletes = []
    restore_calls = []
    monkeypatch.setattr(transport, "_iw_del", lambda iface: deletes.append(iface))
    monkeypatch.setattr(
        transport, "list_phy_ifaces", lambda: {"phy0": ["wlan0"]}
    )
    monkeypatch.setattr(transport, "_iface_exists", lambda _iface: False)
    monkeypatch.setattr(transport.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        live,
        "_restore_radio_state",
        lambda prior_errors=(): restore_calls.append(list(prior_errors)),
    )

    live._teardown_radio()

    assert deletes == []
    assert restore_calls == [[]]


def test_teardown_propagates_target_phy_and_exact_session_owned_vifs(
    monkeypatch,
) -> None:
    live = transport.LiveTransport(phyname="phy7", log=lambda _message: None)
    live._owned_ldn_vifs = {"ldn", "ldnclient"}
    cleanup_calls = []
    monkeypatch.setattr(
        transport,
        "light_cleanup",
        lambda _log, **kwargs: cleanup_calls.append(kwargs),
    )
    monkeypatch.setattr(
        live,
        "_restore_radio_state",
        lambda prior_errors=(): None,
    )

    live._teardown_radio()

    assert cleanup_calls == [{
        "target_phys": {"phy7"},
        "owned_ifaces": {"ldn", "ldnclient"},
    }]


def test_single_interface_stop_is_idempotent_and_never_falls_back_to_vif_delete(
    monkeypatch,
) -> None:
    live = transport.LiveTransport(log=lambda _message: None)
    live._single_interface_active = True
    live._single_interface_session = True
    live._session_ifname = "wlan0"
    live._single_interface_original_addresses = set()
    live._single_interface_original_neighbors = set()
    live._single_interface_original_broadcast_routes = set()
    deletes = []
    inspections = []

    def empty(_iface):
        inspections.append(True)
        return set()

    monkeypatch.setattr(transport, "_iw_del", lambda iface: deletes.append(iface))
    monkeypatch.setattr(
        transport, "list_phy_ifaces", lambda: {"phy0": ["wlan0"]}
    )
    monkeypatch.setattr(transport, "_iface_exists", lambda _iface: False)
    monkeypatch.setattr(transport.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(transport, "interface_ipv4_addresses", empty)
    monkeypatch.setattr(transport, "interface_neighbor_ips", empty)
    monkeypatch.setattr(transport, "interface_local_broadcast_routes", empty)
    monkeypatch.setattr(
        transport, "restore_wireless_interface_types", lambda *_args: None
    )
    monkeypatch.setattr(transport, "restore_network_state", lambda *_args: None)
    monkeypatch.setattr(transport, "restore_active_connections", lambda *_args: None)

    live.stop()
    first_inspections = len(inspections)
    live.stop()

    assert first_inspections == 9
    assert len(inspections) == first_inspections
    assert deletes == []
    assert live._radio_teardown_complete is True


@pytest.mark.parametrize(
    ("single_interface", "expected"),
    [
        (True, "169.254.21.2"),
        (False, "169.254.99.8"),
    ],
)
def test_reused_interface_prefers_participant_ip_but_normal_vif_keeps_behavior(
    monkeypatch, single_interface, expected
) -> None:
    live = transport.LiveTransport(log=lambda _message: None)
    live._single_interface_active = single_interface
    live.host_ip = "169.254.21.1"
    monkeypatch.setattr(live, "_iface_ip", lambda: "169.254.99.8")
    participant = SimpleNamespace(ip_address="169.254.21.2")

    assert live._resolve_our_ip(participant) == expected


def test_thread_start_failure_restores_radio_and_stop_cannot_mask_error(
    monkeypatch,
) -> None:
    live = transport.LiveTransport(single_interface=False, log=lambda _message: None)
    events = []
    monkeypatch.setattr(
        transport, "choose_single_interface", lambda *_args: None
    )

    def free_radio(_phys, _log, restore_state, **_kwargs):
        events.append("free")
        restore_state["wlan0"] = (True, True)

    monkeypatch.setattr(transport, "free_radio", free_radio)
    monkeypatch.setattr(
        transport,
        "light_cleanup",
        lambda _log, **_kwargs: events.append("cleanup"),
    )
    monkeypatch.setattr(
        transport,
        "restore_network_state",
        lambda *_args: events.append("restore"),
    )

    def fail_start(_thread):
        raise RuntimeError("synthetic Thread.start failure")

    monkeypatch.setattr(transport.threading.Thread, "start", fail_start)

    with pytest.raises(RuntimeError, match="synthetic Thread.start failure"):
        live.start(attempts=1)

    assert live._thread is None
    assert events == ["free", "cleanup", "restore"]
    live.stop()
    assert events == ["free", "cleanup", "restore"]


def test_normal_session_reserves_and_owns_custom_vif_before_worker_start(
    monkeypatch,
) -> None:
    live = transport.LiveTransport(
        ifname="za-custom",
        single_interface=False,
        log=lambda _message: None,
    )
    free_calls = []
    cleanup_calls = []
    restore_calls = []
    monkeypatch.setattr(
        transport, "choose_single_interface", lambda *_args: None
    )

    def free_radio(_phys, _log, restore_state, **kwargs):
        free_calls.append(kwargs)
        restore_state["wlan0"] = (True, True)

    monkeypatch.setattr(transport, "free_radio", free_radio)
    monkeypatch.setattr(
        transport,
        "light_cleanup",
        lambda _log, **kwargs: cleanup_calls.append(kwargs),
    )
    monkeypatch.setattr(
        transport,
        "restore_network_state",
        lambda *args: restore_calls.append(args),
    )
    monkeypatch.setattr(
        transport.threading.Thread,
        "start",
        lambda _thread: (_ for _ in ()).throw(
            RuntimeError("synthetic Thread.start failure")
        ),
    )

    with pytest.raises(RuntimeError, match="synthetic Thread.start failure"):
        live.start(attempts=1)

    assert free_calls[0]["reserved_ifaces"] == {"za-custom"}
    assert free_calls[0]["owned_ifaces"] == set()
    assert cleanup_calls == [{
        "target_phys": {"phy0"},
        "owned_ifaces": {"ldn", "za-custom"},
    }]
    assert restore_calls[0][3] == {"ldn", "za-custom"}


def test_free_radio_preflight_failure_releases_unmutated_lease_without_restore(
    monkeypatch,
) -> None:
    live = transport.LiveTransport(single_interface=True, log=lambda _message: None)
    restore_calls = []

    def prepare():
        live._single_interface_active = True
        live._single_interface_session = True
        live._session_ifname = "wlan0"
        live._radio_restore_state = {"wlan0": (True, True)}
        live._single_interface_original_addresses = set()
        live._single_interface_original_neighbors = set()
        live._single_interface_original_broadcast_routes = set()

    monkeypatch.setattr(live, "_prepare_radio_path", prepare)
    def fail_preflight(*_args, **_kwargs):
        raise RuntimeError("synthetic free-radio preflight failure")

    monkeypatch.setattr(transport, "free_radio", fail_preflight)
    monkeypatch.setattr(
        live,
        "_restore_radio_state",
        lambda *_args, **_kwargs: restore_calls.append(True),
    )

    with pytest.raises(RuntimeError, match="free-radio preflight failure"):
        live.start()

    assert restore_calls == []
    assert live._single_interface_active is False
    assert live._radio_restore_state == {}
    assert live._radio_teardown_complete is True


def test_free_radio_failure_after_mutation_restores_before_reraising(
    monkeypatch,
) -> None:
    live = transport.LiveTransport(single_interface=False, log=lambda _message: None)
    events = []
    monkeypatch.setattr(live, "_prepare_radio_path", lambda: None)

    def fail_after_mutation(_phys, _log, restore_state, *, on_mutation, **_kwargs):
        restore_state["wlan0"] = (True, True)
        on_mutation()
        raise RuntimeError("synthetic free-radio mutation failure")

    monkeypatch.setattr(transport, "free_radio", fail_after_mutation)
    monkeypatch.setattr(
        transport,
        "light_cleanup",
        lambda _log, **_kwargs: events.append("cleanup"),
    )
    monkeypatch.setattr(
        transport,
        "restore_network_state",
        lambda *_args: events.append("restore"),
    )

    with pytest.raises(RuntimeError, match="free-radio mutation failure"):
        live.start()

    assert events == ["cleanup", "restore"]
    assert live._radio_restore_state == {}
    assert live._radio_teardown_complete is True


def test_nonretryable_join_error_restores_without_another_attempt(
    monkeypatch,
) -> None:
    live = transport.LiveTransport(single_interface=True, log=lambda _message: None)
    events = []

    def prepare():
        live._single_interface_active = True
        live._single_interface_session = True
        live._session_ifname = "wlan0"

    def failed_worker():
        live._err = "legacy driver lacks custom control-port support"
        live._join_retryable = False
        live._ready.set()

    monkeypatch.setattr(live, "_prepare_radio_path", prepare)
    monkeypatch.setattr(
        transport,
        "free_radio",
        lambda *_args, **_kwargs: events.append("free"),
    )
    monkeypatch.setattr(live, "_run_ldn", failed_worker)
    monkeypatch.setattr(
        live, "_teardown_radio", lambda: events.append("restore")
    )

    with pytest.raises(RuntimeError, match="LDN join cannot continue"):
        live.start(timeout=1, attempts=3, settle=0)

    assert events == ["free", "restore"]


@pytest.mark.parametrize(
    ("driver", "expected_reset", "second_phy"),
    (
        ("rtw88_8812au", True, "phy14"),
        ("mt76x0u", False, "phy13"),
    ),
)
def test_single_interface_silent_monitor_resets_only_allowlisted_usb(
    monkeypatch,
    driver,
    expected_reset,
    second_phy,
) -> None:
    live = transport.LiveTransport(
        phyname="phy13",
        ifname="wlan0",
        single_interface=True,
        log=lambda _message: None,
    )
    free_phys = []
    reset_calls = []
    worker_calls = []

    def prepare() -> None:
        live._single_interface_active = True
        live._single_interface_session = True
        live._session_ifname = "wlan0"
        live._radio_restore_state = {"wlan0": (True, True)}
        live._single_interface_original_addresses = set()
        live._single_interface_original_neighbors = set()
        live._single_interface_original_broadcast_routes = set()
        live._single_interface_original_mac = "02:11:22:33:44:55"
        live._connection_baseline_state = {"wlan0": False}

    def free_radio(phys, *_args, **_kwargs):
        free_phys.append(set(phys))

    def failed_worker() -> None:
        worker_calls.append(True)
        live._err = "synthetic silent monitor"
        live._monitor_receive_silent = True
        live._ready.set()

    def reset(iface, mac, _log):
        reset_calls.append((iface, mac))
        return "phy14"

    monkeypatch.setattr(live, "_prepare_radio_path", prepare)
    monkeypatch.setattr(transport, "free_radio", free_radio)
    monkeypatch.setattr(live, "_run_ldn", failed_worker)
    monkeypatch.setattr(live, "_cleanup_single_interface_network", lambda: None)
    monkeypatch.setattr(
        transport,
        "_usb_wireless_identity",
        lambda _iface: {
            "vendor": "0bda",
            "product": "8812",
            "driver": driver,
        },
    )
    monkeypatch.setattr(transport, "reset_silent_usb_radio", reset)
    monkeypatch.setattr(live, "_teardown_radio", lambda: None)

    with pytest.raises(RuntimeError, match="LDN join failed after 2 attempt"):
        live.start(timeout=1, attempts=2, settle=0)

    assert len(worker_calls) == 2
    assert free_phys == [{"phy13"}, {second_phy}]
    assert reset_calls == (
        [("wlan0", "02:11:22:33:44:55")] if expected_reset else []
    )
    assert live._silent_usb_recovery_attempted is True


def test_single_interface_retry_excludes_failed_ssid_for_only_next_attempt(
    monkeypatch,
) -> None:
    live = transport.LiveTransport(single_interface=True, log=lambda _message: None)
    failed_ssid = bytes.fromhex("00112233445566778899aabbccddeeff")
    exclusions = []

    def prepare() -> None:
        live._single_interface_active = True
        live._single_interface_session = True
        live._session_ifname = "wlan0"

    def failed_worker() -> None:
        exclusions.append(live._attempt_excluded_ssid)
        if len(exclusions) == 1:
            # Only an actual connection failure publishes an identity.  The
            # second synthetic failure represents a scan with no fresh room.
            live._attempt_failed_ssid = failed_ssid
        live._err = "synthetic retryable failure"
        live._ready.set()

    # A previous start must not leak either slot into this invocation.
    live._attempt_excluded_ssid = b"stale exclusion"
    live._attempt_failed_ssid = b"stale failure"
    monkeypatch.setattr(live, "_prepare_radio_path", prepare)
    monkeypatch.setattr(transport, "free_radio", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(live, "_run_ldn", failed_worker)
    monkeypatch.setattr(live, "_cleanup_single_interface_network", lambda: None)
    monkeypatch.setattr(live, "_teardown_radio", lambda: None)

    with pytest.raises(RuntimeError, match="LDN join failed after 3 attempt"):
        live.start(timeout=1, attempts=3, settle=0)

    assert exclusions == [None, failed_ssid, None]


def test_single_interface_run_ldn_skips_failed_ssid_then_records_rotated_failure(
    monkeypatch,
) -> None:
    fake_ldn = _install_fake_ldn_runtime(monkeypatch)
    live = transport.LiveTransport(
        single_interface=True,
        require_exact_network=True,
        log=lambda _message: None,
    )
    live._single_interface_active = True
    live._session_ifname = "wlan0"
    address = b"\xaa\xbb\xcc\xdd\xee\xff"
    failed_ssid = bytes.fromhex("00112233445566778899aabbccddeeff")
    rotated_ssid = bytes.fromhex("102132435465768798a9bacbdcedfe0f")

    def network(ssid):
        return SimpleNamespace(
            accept_policy=1,
            num_participants=1,
            max_participants=8,
            local_communication_id=live.LOCAL_COMMUNICATION_ID,
            scene_id=live.SCENE_ID,
            application_data=b"",
            address=address,
            ssid=ssid,
            channel=6,
            app_version=1,
        )

    stale = network(failed_ssid)
    rotated = network(rotated_ssid)
    predicate_results = []

    async def scan(_ldn, _keys, **kwargs):
        predicate = kwargs["stop_when"]
        predicate_results.extend((predicate(stale), predicate(rotated)))
        return [rotated]

    class FailedConnection:
        async def __aenter__(self):
            raise ConnectionError("synthetic WLAN connection failure")

        async def __aexit__(self, *_args):
            return False

    connect_networks = []

    def connect(_ldn, param, **_kwargs):
        connect_networks.append(param.network)
        return FailedConnection()

    monkeypatch.setattr(ldn_single, "scan", scan)
    monkeypatch.setattr(ldn_single, "connect", connect)
    live._attempt_excluded_ssid = failed_ssid

    live._run_ldn()

    assert predicate_results == [False, True]
    assert connect_networks == [rotated]
    assert live._attempt_failed_ssid == rotated_ssid
    assert "synthetic WLAN connection failure" in live._err
    assert isinstance(live._join_exception, ConnectionError)
    assert fake_ldn.ACCEPT_NONE == 0


def test_connected_worker_stop_uses_graceful_real_trio_unwind(monkeypatch) -> None:
    trio = pytest.importorskip("trio")
    fake_ldn = SimpleNamespace(
        ACCEPT_NONE=0,
        ConnectNetworkParam=SimpleNamespace,
        load_keys=lambda _path: {},
    )
    monkeypatch.setitem(sys.modules, "trio", trio)
    monkeypatch.setitem(sys.modules, "ldn", fake_ldn)
    events: list[str] = []
    live = transport.LiveTransport(
        nickname="ZATRADE",
        single_interface=True,
        require_exact_network=True,
        app_data_decoder=lambda data, _log: data,
        log=lambda _message: None,
    )
    live._single_interface_active = True
    live._session_ifname = "wlan0"
    live._attempt_stop = transport.threading.Event()
    host = SimpleNamespace(
        name=b"HOST\0",
        ip_address="169.254.42.1",
        mac_address=b"\xaa\xbb\xcc\xdd\xee\xff",
        connected=True,
    )
    ours = SimpleNamespace(
        name=b"ZATRADE\0",
        ip_address="169.254.42.2",
        mac_address=b"\x00\x11\x22\x33\x44\x55",
        connected=True,
    )
    network = SimpleNamespace(
        accept_policy=1,
        num_participants=1,
        max_participants=2,
        local_communication_id=live.LOCAL_COMMUNICATION_ID,
        scene_id=live.SCENE_ID,
        application_data=b"room",
        address=host.mac_address,
        ssid=bytes.fromhex("00112233445566778899aabbccddeeff"),
        channel=6,
        app_version=1,
        participants=[host, ours],
    )

    async def scan(_ldn, _keys, **kwargs):
        assert kwargs["stop_when"](network) is True
        return [network]

    class JoinedNetwork:
        def info(self):
            return network

        async def next_event(self):
            await trio.sleep_forever()

    @asynccontextmanager
    async def joined_connection(_ldn, _param, **_kwargs):
        events.append("connection-enter")
        try:
            yield JoinedNetwork()
        finally:
            events.append("cleanup-start")
            try:
                # Old watch_stop cancelled the whole nursery after about
                # 50 ms, so this checkpoint made the lost-netlink-receiver
                # cleanup race deterministic.
                await trio.sleep(0.1)
            except trio.Cancelled:
                events.append("cleanup-cancelled")
                raise
            events.append("cleanup-complete")

    monkeypatch.setattr(ldn_single, "scan", scan)
    monkeypatch.setattr(ldn_single, "connect", joined_connection)
    worker = transport.threading.Thread(target=live._run_ldn)
    worker.start()
    assert live._ready.wait(1.0)
    assert live._err is None

    live._attempt_stop.set()
    worker.join(1.5)

    assert not worker.is_alive()
    assert events == [
        "connection-enter",
        "cleanup-start",
        "cleanup-complete",
    ]
    assert live._err is None


def test_run_ldn_marks_complete_monitor_silence_for_recovery(monkeypatch) -> None:
    _install_fake_ldn_runtime(monkeypatch)
    live = transport.LiveTransport(
        single_interface=True,
        require_exact_network=True,
        log=lambda _message: None,
    )
    live._single_interface_active = True
    live._session_ifname = "wlan0"

    async def silent_scan(*_args, **_kwargs):
        raise ldn_single.NoMonitorFramesError("synthetic monitor silence")

    monkeypatch.setattr(ldn_single, "scan", silent_scan)

    live._run_ldn()

    assert live._monitor_receive_silent is True
    assert live._err is not None
    assert "synthetic monitor silence" in live._err


def test_single_interface_scan_failure_does_not_record_attempted_ssid(
    monkeypatch,
) -> None:
    _install_fake_ldn_runtime(monkeypatch)
    live = transport.LiveTransport(
        single_interface=True,
        require_exact_network=True,
        log=lambda _message: None,
    )
    live._single_interface_active = True
    live._session_ifname = "wlan0"
    failed_ssid = bytes.fromhex("00112233445566778899aabbccddeeff")
    stale = SimpleNamespace(
        accept_policy=1,
        num_participants=1,
        max_participants=8,
        local_communication_id=live.LOCAL_COMMUNICATION_ID,
        scene_id=live.SCENE_ID,
        application_data=b"",
        address=b"\xaa\xbb\xcc\xdd\xee\xff",
        ssid=failed_ssid,
        channel=6,
        app_version=1,
    )

    async def scan(_ldn, _keys, **kwargs):
        assert kwargs["stop_when"](stale) is False
        return [stale]

    def unexpected_connect(*_args, **_kwargs):
        pytest.fail("an excluded scan result must not reach connect")

    monkeypatch.setattr(ldn_single, "scan", scan)
    monkeypatch.setattr(ldn_single, "connect", unexpected_connect)
    live._attempt_excluded_ssid = failed_ssid

    live._run_ldn()

    assert live._attempt_failed_ssid is None
    assert live._err is not None
    assert "no exact joinable" in live._err


def test_start_refuses_to_overwrite_incomplete_radio_restore_lease(
    monkeypatch,
) -> None:
    live = transport.LiveTransport(single_interface=False, log=lambda _message: None)
    live._radio_restore_state = {"wlan0": (True, True)}
    preflight_calls = []
    monkeypatch.setattr(
        live, "_prepare_radio_path", lambda: preflight_calls.append(True)
    )

    with pytest.raises(RuntimeError, match="previous radio restoration lease"):
        live.start()

    assert preflight_calls == []
    assert live._radio_restore_state == {"wlan0": (True, True)}


def test_successful_restore_releases_lease_and_second_restore_is_noop(
    monkeypatch,
) -> None:
    live = transport.LiveTransport(log=lambda _message: None)
    live._single_interface_active = True
    live._session_ifname = "wlan0"
    live._single_interface_original_addresses = set()
    live._single_interface_original_neighbors = set()
    live._single_interface_original_broadcast_routes = set()
    inspections = []

    def empty(_iface):
        inspections.append(True)
        return set()

    monkeypatch.setattr(transport, "interface_ipv4_addresses", empty)
    monkeypatch.setattr(transport, "interface_neighbor_ips", empty)
    monkeypatch.setattr(transport, "interface_local_broadcast_routes", empty)
    monkeypatch.setattr(
        transport, "restore_wireless_interface_types", lambda *_args: None
    )
    monkeypatch.setattr(transport, "restore_network_state", lambda *_args: None)
    monkeypatch.setattr(transport, "restore_active_connections", lambda *_args: None)

    live._restore_radio_state()
    first_count = len(inspections)
    live._restore_radio_state()

    assert first_count == 9
    assert len(inspections) == first_count
    assert live._single_interface_active is False


@pytest.mark.parametrize(
    ("missing_kind", "message"),
    [
        ("address", "original IPv4 addresses are missing"),
        ("neighbor", "original IPv4 neighbors are missing"),
        ("route", "original local broadcast routes are missing"),
    ],
)
def test_final_restore_retains_lease_when_original_network_state_is_missing(
    monkeypatch, missing_kind, message
) -> None:
    live = transport.LiveTransport(log=lambda _message: None)
    live._single_interface_active = True
    live._session_ifname = "wlan0"
    original_addresses = {"192.0.2.2/24"}
    original_neighbors = {"192.0.2.1"}
    original_routes = {"192.0.2.255"}
    live._single_interface_original_addresses = set(original_addresses)
    live._single_interface_original_neighbors = set(original_neighbors)
    live._single_interface_original_broadcast_routes = set(original_routes)
    live._radio_restore_state = {"wlan0": (True, True)}
    live._connection_restore_state = {"wlan0": "profile-uuid"}
    live._connection_baseline_state = {"wlan0": "profile-uuid"}
    live._autoconnect_baseline_state = {"wlan0": True}

    current_addresses = set() if missing_kind == "address" else original_addresses
    current_neighbors = set() if missing_kind == "neighbor" else original_neighbors
    current_routes = set() if missing_kind == "route" else original_routes
    monkeypatch.setattr(
        transport, "interface_ipv4_addresses", lambda _iface: set(current_addresses)
    )
    monkeypatch.setattr(
        transport, "interface_neighbor_ips", lambda _iface: set(current_neighbors)
    )
    monkeypatch.setattr(
        transport,
        "interface_local_broadcast_routes",
        lambda _iface: set(current_routes),
    )
    monkeypatch.setattr(
        transport, "restore_wireless_interface_types", lambda *_args: None
    )
    monkeypatch.setattr(transport, "restore_network_state", lambda *_args: None)
    monkeypatch.setattr(transport, "restore_active_connections", lambda *_args: None)
    monkeypatch.setattr(transport, "_interface_state", lambda _iface: (True, True))
    monkeypatch.setattr(
        transport, "interface_active_connection", lambda _iface: "profile-uuid"
    )
    monkeypatch.setattr(transport, "interface_autoconnect", lambda _iface: True)
    monkeypatch.setattr(
        transport,
        "_run",
        lambda _command: SimpleNamespace(returncode=0),
    )

    with pytest.raises(RuntimeError, match=message):
        live._restore_radio_state()

    assert live._single_interface_active is True
    assert live._single_interface_original_addresses == original_addresses
    assert live._single_interface_original_neighbors == original_neighbors
    assert live._single_interface_original_broadcast_routes == original_routes
    assert live._radio_restore_state == {"wlan0": (True, True)}
    assert live._connection_restore_state == {"wlan0": "profile-uuid"}
    assert live._connection_baseline_state == {"wlan0": "profile-uuid"}
    assert live._autoconnect_baseline_state == {"wlan0": True}


def test_final_restore_accepts_original_network_state_recreated_by_reconnect(
    monkeypatch,
) -> None:
    live = transport.LiveTransport(log=lambda _message: None)
    live._single_interface_active = True
    live._session_ifname = "wlan0"
    original_addresses = {"192.0.2.2/24"}
    original_neighbors = {"192.0.2.1"}
    original_routes = {"192.0.2.255"}
    live._single_interface_original_addresses = set(original_addresses)
    live._single_interface_original_neighbors = set(original_neighbors)
    live._single_interface_original_broadcast_routes = set(original_routes)
    live._connection_restore_state = {"wlan0": "profile-uuid"}
    live._connection_baseline_state = {"wlan0": "profile-uuid"}
    reconnected = False

    def current(original):
        return set(original) if reconnected else set()

    monkeypatch.setattr(
        transport,
        "interface_ipv4_addresses",
        lambda _iface: current(original_addresses),
    )
    monkeypatch.setattr(
        transport,
        "interface_neighbor_ips",
        lambda _iface: current(original_neighbors),
    )
    monkeypatch.setattr(
        transport,
        "interface_local_broadcast_routes",
        lambda _iface: current(original_routes),
    )
    monkeypatch.setattr(
        transport, "restore_wireless_interface_types", lambda *_args: None
    )
    monkeypatch.setattr(transport, "restore_network_state", lambda *_args: None)

    def restore_connections(*_args):
        nonlocal reconnected
        reconnected = True

    monkeypatch.setattr(transport, "restore_active_connections", restore_connections)
    monkeypatch.setattr(
        transport, "interface_active_connection", lambda _iface: "profile-uuid"
    )

    live._restore_radio_state()

    assert reconnected is True
    assert live._single_interface_active is False
    assert live._single_interface_original_addresses is None
    assert live._single_interface_original_neighbors is None
    assert live._single_interface_original_broadcast_routes is None
    assert live._connection_restore_state == {}
    assert live._connection_baseline_state == {}


def test_disconnected_baseline_is_not_left_autoconnected(monkeypatch) -> None:
    live = transport.LiveTransport(log=lambda _message: None)
    live._single_interface_active = True
    live._session_ifname = "wlan0"
    live._single_interface_original_addresses = set()
    live._single_interface_original_neighbors = set()
    live._single_interface_original_broadcast_routes = set()
    live._connection_baseline_state = {"wlan0": False}
    live._autoconnect_baseline_state = {"wlan0": True}
    monkeypatch.setattr(transport, "interface_ipv4_addresses", lambda _iface: set())
    monkeypatch.setattr(transport, "interface_neighbor_ips", lambda _iface: set())
    monkeypatch.setattr(
        transport, "interface_local_broadcast_routes", lambda _iface: set()
    )
    monkeypatch.setattr(
        transport, "restore_wireless_interface_types", lambda *_args: None
    )
    monkeypatch.setattr(transport, "restore_network_state", lambda *_args: None)
    monkeypatch.setattr(transport, "restore_active_connections", lambda *_args: None)
    connection_states = iter([False])
    monkeypatch.setattr(
        transport, "interface_active_connection", lambda _iface: next(connection_states)
    )
    monkeypatch.setattr(transport, "interface_autoconnect", lambda _iface: True)
    commands = []
    monkeypatch.setattr(
        transport,
        "_run",
        lambda command: commands.append(command) or SimpleNamespace(returncode=0),
    )

    live._restore_radio_state()

    assert ["nmcli", "device", "disconnect", "wlan0"] in commands
    assert [
        "nmcli", "device", "set", "wlan0", "autoconnect", "no"
    ] in commands
    assert [
        "nmcli", "device", "set", "wlan0", "autoconnect", "yes"
    ] in commands
    assert live._connection_baseline_state == {}
    assert live._autoconnect_baseline_state == {}
    assert live._single_interface_active is False


@pytest.mark.parametrize("final_up", [True, None])
def test_final_link_readback_failure_is_distinct_from_earlier_restore_success(
    monkeypatch, final_up,
) -> None:
    logs = []
    live = transport.LiveTransport(log=logs.append)
    live._single_interface_active = True
    live._session_ifname = "wlan0"
    live._single_interface_original_addresses = set()
    live._single_interface_original_neighbors = set()
    live._single_interface_original_broadcast_routes = set()
    live._radio_restore_state = {"wlan0": (True, False)}
    monkeypatch.setattr(live, "_cleanup_single_interface_network", lambda **_: None)
    monkeypatch.setattr(transport, "interface_ipv4_addresses", lambda _: set())
    monkeypatch.setattr(transport, "interface_neighbor_ips", lambda _: set())
    monkeypatch.setattr(transport, "interface_local_broadcast_routes", lambda _: set())
    monkeypatch.setattr(transport, "restore_wireless_interface_types", lambda *_: None)
    monkeypatch.setattr(transport, "restore_active_connections", lambda *_: None)
    commands = []
    monkeypatch.setattr(transport, "_run", lambda cmd: commands.append(cmd)
                        or SimpleNamespace(returncode=0))
    # First readback in real restore_network_state passes; later final readback
    # changes/fails. No worker, NM daemon, interface, or radio is involved.
    states = iter([(True, False), (True, final_up)])
    monkeypatch.setattr(transport, "_interface_state", lambda _: next(states))

    with pytest.raises(RuntimeError, match="administrative link state was not restored"):
        live._restore_radio_state()

    assert logs == [
        "[live] restored host interface state for wlan0",
        "[diag] radio cleanup link mismatch phase=final-readback "
        f"expected_managed=True actual_managed=True expected_up=False actual_up={final_up}",
    ]
    assert commands == [["nmcli", "device", "set", "wlan0", "managed", "yes"],
                        ["ip", "link", "set", "wlan0", "down"]]
    assert live._single_interface_active is True
    assert live._radio_restore_state == {"wlan0": (True, False)}
