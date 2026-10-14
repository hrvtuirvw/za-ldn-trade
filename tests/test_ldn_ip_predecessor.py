"""Real upstream WLAN/IP lifecycle, in-memory backends; no sockets or radios.

The seeded-prime marker is not CONNECT submission. Distinguish the pending
predecessor, then verify that IP installation is host-advertisement-owned.
"""
from types import SimpleNamespace as NS

import sys
import pytest

if not sys.platform.startswith("linux") or sys.version_info < (3, 12):
    pytest.skip("Linux live-backend regressions require Python 3.12+", allow_module_level=True)
ldn = pytest.importorskip("ldn", reason="install the Linux .[live,test] dependencies")
import trio
from trio.testing import MockClock

from zasim.ldn import ldn_single

NL = ldn.wlan.nl80211
LOCAL = b"\x02\x00\x00\x00\x00\x02"
HOST = b"\x02\x00\x00\x00\x00\x01"


def wlan_case(stop, *, pin=True, failure=None, extra_outcome=False):
    logs, calls, commands = [], [], []
    entered = trio.Event()

    async def checkpoint(stage):
        calls.append(stage)
        if stop == stage:
            if failure is not None:
                raise failure
            await trio.sleep_forever()
        await trio.lowlevel.checkpoint()

    class Backend:
        delivered = 0

        async def request(self, command, attrs):
            commands.append((command, attrs))
            if command == NL.NL80211_CMD_CONNECT:
                await checkpoint("request")
            elif command == NL.NL80211_CMD_NEW_KEY:
                await checkpoint("key")
            elif command == NL.NL80211_CMD_REGISTER_FRAME:
                await checkpoint("register")
            return []

        async def receive(self):
            if self.delivered >= (2 if extra_outcome else 1):
                await trio.sleep_forever()
            if self.delivered == 1:
                # Make the second outcome strictly post-entry, not dependent
                # on the background receiver/registration scheduling order.
                await entered.wait()
            await checkpoint("event")
            self.delivered += 1
            return NS(type=NL.NL80211_CMD_CONNECT, attributes={
                NL.NL80211_ATTR_IFINDEX: 7,
                NL.NL80211_ATTR_MAC: HOST,
                NL.NL80211_ATTR_STATUS_CODE: 0,
            })

    class Station(ldn.wlan.Station):
        def __init__(self, wlan, router, name, index, address, ssid, channel, key):
            # Avoid Interface.__init__'s real socket construction. All lifecycle
            # methods below remain upstream, except for UP/sysctl host effects.
            self._wlan, self._router = wlan, router
            self._name, self._index, self._address = name, index, address
            self._ssid, self._channel, self._key = ssid, channel, key
            self._host_address = None
            self._events = ldn.queue.create()

        async def up(self):
            await checkpoint("up")

        def disable_ipv6(self):
            calls.append("ipv6")

    module = NS(trio=trio, wlan=NS(**dict(vars(ldn.wlan), Station=Station)))

    async def run():
        factory = ldn_single.ExistingInterfaceFactory(
            module, NS(_wlan=Backend(), _router=None), "synthetic", log=logs.append,
        )

        async def attributes(_phy):
            return {NL.NL80211_ATTR_IFINDEX: 7, NL.NL80211_ATTR_MAC: LOCAL}

        async def noop(*_args, **_kwargs):
            pass

        async def prime(*_args):
            # This is where the actual live wrapper logs its premature marker.
            logs.append("synthetic old preflight CONNECT request marker")
            await checkpoint("prime")

        async def down(*_args):
            calls.append("down")

        factory._attributes = attributes
        factory._require_ldn_control_port = factory._set_type = noop
        factory._prime_station_bss = prime
        factory._down = down
        with trio.move_on_after(1):
            try:
                async with factory.connect_network(
                    "synthetic-phy", "synthetic", "synthetic-ssid", 6,
                    b"synthetic-key", HOST if pin else None,
                ):
                    calls.append("entered")
                    entered.set()
                    if extra_outcome:
                        await trio.sleep(.01)
            except OSError as exc:
                assert exc is failure

    trio.run(run, clock=MockClock(autojump_threshold=0))
    return logs, calls, commands


@pytest.mark.parametrize("stop,stage,ack", [
    ("prime", "station-setup", False),
    ("up", "station-start", False),
    ("request", "connect-request-pending", False),
    ("event", "connect-event-pending", True),
    ("key", "connect-result-received", True),
    ("register", "connect-result-received", True),
    (None, "wlan-ready", True),
])
def test_real_upstream_pending_boundary_survives_cancellation(stop, stage, ack):
    logs, calls, commands = wlan_case(stop)
    assert "synthetic old preflight CONNECT request marker" in logs
    assert [s for s in logs if "handoff exit" in s] == [
        f"[diag] LDN WLAN handoff exit stage={stage}",
    ]
    begin = "[diag] LDN CONNECT request boundary phase=awaiting-ack"
    end = "[diag] LDN CONNECT request boundary phase=acknowledged"
    assert (begin in logs) == (stop not in ("prime", "up"))
    assert (end in logs) == ack
    assert calls[-1] == "down"
    assert ("entered" in calls) == (stop is None)
    connect = [a for c, a in commands if c == NL.NL80211_CMD_CONNECT]
    assert len(connect) == int(stop not in ("prime", "up"))
    if connect:
        assert connect[0][NL.NL80211_ATTR_MAC] == HOST
        assert connect[0][NL.NL80211_ATTR_CONTROL_PORT] is True
        assert connect[0][NL.NL80211_ATTR_CONTROL_PORT_OVER_NL80211] is True
    assert "synthetic-key" not in "\n".join(logs)
    assert "synthetic-ssid" not in "\n".join(logs)


@pytest.mark.parametrize("pin", [False, True])
def test_failed_request_does_not_fabricate_an_ack_or_association(pin):
    logs, calls, _ = wlan_case("request", pin=pin, failure=OSError("private error"))
    assert not any("phase=acknowledged" in s for s in logs)
    assert logs[-1].endswith("stage=connect-request-pending")
    assert "private error" not in "\n".join(logs)
    assert calls[-1] == "down"


def test_unpinned_upstream_outcome_also_advances_boundary():
    logs, calls, commands = wlan_case("key", pin=False)
    assert logs[-1].endswith("stage=connect-result-received")
    attrs = next(a for c, a in commands if c == NL.NL80211_CMD_CONNECT)
    assert NL.NL80211_ATTR_MAC not in attrs
    assert calls[-1] == "down"


@pytest.mark.parametrize("pin", [False, True])
def test_late_connect_event_cannot_regress_ready_stage(pin):
    logs, calls, _ = wlan_case(None, pin=pin, extra_outcome=True)
    assert calls.count("event") == 2
    assert logs[-1].endswith("stage=wlan-ready")


@pytest.mark.parametrize("upstream", [False, True])
@pytest.mark.parametrize("assigned", [False, True])
def test_ip_and_static_neighbors_come_only_from_host_participant_table(upstream, assigned):
    """Actual Station/Interface netlink hooks: no DHCP or ARP RX prerequisite."""
    calls = []
    route = ldn.wlan.route

    class Backend:
        async def request(self, command, attrs):
            calls.append(("wlan", command, attrs))
            return []

    class Router:
        async def add_address(self, *args):
            calls.append(("address", args))

        async def add_neighbor(self, *args):
            calls.append(("neighbor", args))

    async def run():
        interface = object.__new__(ldn.wlan.Station)
        interface._wlan, interface._router = Backend(), Router()
        interface._index, interface._host_address = 7, HOST
        interface._address = ldn.wlan.MACAddress(LOCAL)
        network = ldn.NetworkInfo(3)
        network.participants = [
            NS(ip_address="169.254.42.1", mac_address=ldn.wlan.MACAddress(HOST), connected=True),
            NS(ip_address="169.254.42.2", mac_address=ldn.wlan.MACAddress(LOCAL), connected=True),
        ]
        station = object.__new__(ldn.STANetwork)
        station._interface, station._advertisements = interface, ldn.queue.create()
        if assigned:
            await station._advertisements.put(network)
        if upstream:
            operation = lambda: station._initialize_network()
        else:
            operation = lambda: ldn_single._initialize_single_interface_station(
                station, ldn, timeout=5, log=lambda _: None,
            )
        if assigned:
            await operation()
            assert station._network is network
            assert station._participant_id == 1
            assert station._network_id == 42
        else:
            with pytest.raises(ConnectionError, match="Failed to obtain IP address"):
                await operation()

    trio.run(run, clock=MockClock(autojump_threshold=0))
    assert calls[0][0:2] == ("wlan", NL.NL80211_CMD_SET_STATION)
    if not assigned:
        assert len(calls) == 1  # No IP/neighbor operation was ever reached.
        return
    assert [c[0] for c in calls] == ["wlan", "address", "neighbor", "neighbor"]
    af, prefix, flags, scope, index, attrs = calls[1][1]
    assert prefix == 24 and flags == route.IFA_F_PERMANENT and index == 7
    assert attrs[route.IFA_LOCAL] == b"\xa9\xfe\x2a\x02"
    assert attrs[route.IFA_BROADCAST] == b"\xa9\xfe\x2a\xff"
    for call, mac, suffix in zip(calls[2:], (HOST, LOCAL), (1, 2)):
        af, index, state, flags, kind, attrs = call[1]
        assert state == route.NUD_PERMANENT and index == 7
        assert attrs[route.NDA_DST] == bytes((169, 254, 42, suffix))
        assert attrs[route.NDA_LLADDR] == mac
