from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from types import SimpleNamespace

import sys
import pytest

if not sys.platform.startswith("linux") or sys.version_info < (3, 12):
    pytest.skip("Linux live-backend regressions require Python 3.12+", allow_module_level=True)
ldn = pytest.importorskip("ldn", reason="install the Linux .[live,test] dependencies")

from zasim.ldn import ldn_single


@pytest.fixture(autouse=True)
def _synthetic_backend_lifetime(monkeypatch):
    # These unit fakes run under asyncio and have no receiver tasks.  Real
    # Trio cancellation/lifetime is covered in test_ldn_factory_lifetime.py.
    @asynccontextmanager
    async def passthrough(wlan_module, _trio_module):
        async with wlan_module.create_factory() as factory:
            yield factory

    monkeypatch.setattr(ldn_single, "_keep_factory_receivers_alive", passthrough)


class _CancelScope:
    def __init__(self, *, shield=False):
        self.shield = shield

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Socket:
    def __init__(self, events, *, fail=False):
        self.events = events
        self.fail = fail

    async def aclose(self):
        self.events.append("socket-close")
        if self.fail:
            raise RuntimeError("monitor socket close failed")


def _fake_ldn(
    *,
    monitor_error=None,
    custom_control_port=True,
    control_port_over_nl80211=True,
    include_ext_features=True,
    connect_messages=None,
):
    events = []
    nl = SimpleNamespace(
        NL80211_CMD_GET_INTERFACE=1,
        NL80211_CMD_SET_INTERFACE=2,
        NL80211_CMD_NEW_INTERFACE=3,
        NL80211_CMD_DEL_INTERFACE=4,
        NL80211_CMD_DISCONNECT=5,
        NL80211_CMD_GET_WIPHY=6,
        NL80211_CMD_CONNECT=7,
        NL80211_ATTR_IFINDEX=10,
        NL80211_ATTR_IFTYPE=11,
        NL80211_ATTR_MAC=12,
        NL80211_ATTR_WIPHY=13,
        NL80211_ATTR_CONTROL_PORT_ETHERTYPE=14,
        NL80211_ATTR_EXT_FEATURES=15,
        NL80211_ATTR_MNTR_FLAGS=16,
        NL80211_IFTYPE_MONITOR=20,
        NL80211_IFTYPE_STATION=21,
        NL80211_MNTR_FLAG_OTHER_BSS=1,
    )

    class Message:
        @property
        def attributes(self):
            return {
                nl.NL80211_ATTR_IFINDEX: 7,
                nl.NL80211_ATTR_IFTYPE: netlink.interface_type,
                nl.NL80211_ATTR_MAC: b"\x00\x11\x22\x33\x44\x55",
                nl.NL80211_ATTR_WIPHY: 9,
            }

    class WiphyMessage:
        @property
        def attributes(self):
            ext_features = bytearray(4)
            if control_port_over_nl80211:
                bit = ldn_single.CONTROL_PORT_OVER_NL80211_EXT_FEATURE_BIT
                ext_features[bit // 8] |= 1 << (bit % 8)
            attributes = {nl.NL80211_ATTR_WIPHY: 9}
            if include_ext_features:
                attributes[nl.NL80211_ATTR_EXT_FEATURES] = bytes(ext_features)
            if custom_control_port:
                attributes[nl.NL80211_ATTR_CONTROL_PORT_ETHERTYPE] = b""
            return attributes

    class Netlink:
        interface_type = nl.NL80211_IFTYPE_STATION

        def __init__(self):
            self.connect_messages = list(connect_messages or [])

        async def request(self, command, attributes):
            events.append(("request", command, dict(attributes)))
            if command == nl.NL80211_CMD_GET_WIPHY:
                return [WiphyMessage()]
            if command == nl.NL80211_CMD_GET_INTERFACE:
                return [Message()]
            if command == nl.NL80211_CMD_SET_INTERFACE:
                self.interface_type = attributes[nl.NL80211_ATTR_IFTYPE]
            return []

        async def receive(self):
            if not self.connect_messages:
                raise AssertionError("no synthetic nl80211 message remains")
            return self.connect_messages.pop(0)

    netlink = Netlink()

    class Router:
        async def update_link(self, *args):
            events.append(("link", args))

    class Factory:
        _wlan = netlink
        _router = Router()

        async def _get_wiphy_index(self, name):
            assert name == "phy0"
            return 9

    @asynccontextmanager
    async def create_factory():
        yield Factory()

    class Monitor:
        def __init__(self, *_args):
            if monitor_error == "constructor":
                raise RuntimeError("monitor constructor failed")
            self._socket = _Socket(events, fail=monitor_error == "close")

        async def activate(self):
            events.append("monitor-activate")
            if monitor_error == "activate":
                raise RuntimeError("monitor activation failed")

        async def set_channel(self, channel):
            events.append(("channel", channel))

    class Station:
        def __init__(self, wlan, *_args):
            self._wlan = wlan
            self._socket = _Socket(events)

        async def up(self):
            events.append("station-up")

        @asynccontextmanager
        async def connect(self):
            events.append("station-connect")
            await self._wlan.request(nl.NL80211_CMD_CONNECT, {99: b"connect"})
            if connect_messages is not None:
                message = await self._wlan.receive()
                events.append(("station-connect-event", message.attributes))
            try:
                yield
            finally:
                await self._wlan.request(
                    nl.NL80211_CMD_DISCONNECT,
                    {nl.NL80211_ATTR_IFINDEX: 7},
                )
                events.append("station-disconnect")

    class KeyDerivation:
        def __init__(self, *_args, **_kwargs):
            pass

        def derive_data_key(self, _random, _password):
            return b"key"

    class Scanner:
        def __init__(self, _keys, _monitor):
            pass

        async def scan(self, channels, dwell_time):
            events.append(("scan", channels, dwell_time))
            return ["network"]

    class STANetwork:
        def __init__(self, _interface, _param, _keys):
            pass

        @asynccontextmanager
        async def start(self):
            events.append("ldn-start")
            yield self

    wlan = SimpleNamespace(
        nl80211=nl,
        IFF_UP=1,
        MACAddress=lambda value: bytes(value),
        Monitor=Monitor,
        Station=Station,
        Scanner=Scanner,
        STANetwork=STANetwork,
        create_factory=create_factory,
        is_valid_channel=lambda channel: channel in (1, 6, 11, 36, 40, 44, 48),
    )
    module = SimpleNamespace(
        wlan=wlan,
        trio=SimpleNamespace(
            CancelScope=_CancelScope,
            move_on_after=lambda _seconds: _CancelScope(),
        ),
        KeyDerivation=KeyDerivation,
        Scanner=Scanner,
        STANetwork=STANetwork,
        SECURITY_MODE_PROD=1,
    )
    return module, nl, netlink, events


def test_scan_reuses_existing_interface_without_vif_commands(monkeypatch) -> None:
    module, nl, netlink, events = _fake_ldn()
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    monkeypatch.setattr(ldn_single, "_interface_was_up", lambda _name: True)

    result = asyncio.run(ldn_single.scan(
        module, {}, phyname="phy0", ifname="wlx001122334455"
    ))

    assert result == ["network"]
    assert ("scan", [1, 6, 11], 0.500) in events
    commands = [event[1] for event in events if event[0] == "request"]
    assert nl.NL80211_CMD_NEW_INTERFACE not in commands
    assert nl.NL80211_CMD_DEL_INTERFACE not in commands
    set_requests = [
        event[2] for event in events
        if event[:2] == ("request", nl.NL80211_CMD_SET_INTERFACE)
    ]
    assert [request[nl.NL80211_ATTR_IFTYPE] for request in set_requests] == [
        nl.NL80211_IFTYPE_MONITOR,
        nl.NL80211_IFTYPE_STATION,
    ]
    assert set_requests[0][nl.NL80211_ATTR_MNTR_FLAGS] == {
        nl.NL80211_MNTR_FLAG_OTHER_BSS: True,
    }
    assert nl.NL80211_ATTR_MNTR_FLAGS not in set_requests[1]
    assert set(set_requests[0]) == {
        nl.NL80211_ATTR_IFINDEX,
        nl.NL80211_ATTR_IFTYPE,
        nl.NL80211_ATTR_MNTR_FLAGS,
    }
    assert set(set_requests[1]) == {
        nl.NL80211_ATTR_IFINDEX,
        nl.NL80211_ATTR_IFTYPE,
        nl.NL80211_ATTR_MAC,
    }
    assert set_requests[1][nl.NL80211_ATTR_MAC] == b"\x00\x11\x22\x33\x44\x55"
    assert any(
        event[0] == "link" and event[1][3:5] == (module.wlan.IFF_UP, module.wlan.IFF_UP)
        for event in events
    )
    assert netlink.interface_type == nl.NL80211_IFTYPE_STATION


def test_complete_scan_with_no_raw_frames_requests_radio_recovery(
    monkeypatch,
) -> None:
    module, _nl, netlink, events = _fake_ldn()
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)

    class EmptyScanner:
        def __init__(self, _keys, _monitor):
            pass

        async def scan(self, channels, dwell_time):
            events.append(("scan", channels, dwell_time))
            return []

    module.Scanner = EmptyScanner
    logs = []

    with pytest.raises(
        ldn_single.NoMonitorFramesError,
        match="without receiving any 802.11 frame",
    ):
        asyncio.run(ldn_single.scan(
            module,
            {},
            phyname="phy0",
            ifname="wlx001122334455",
            log=logs.append,
        ))

    assert any("0 monitor frame(s)" in message for message in logs)
    assert netlink.interface_type == module.wlan.nl80211.NL80211_IFTYPE_STATION


def test_targeted_scan_returns_immediately_on_matching_advertisement(
    monkeypatch,
) -> None:
    module, nl, netlink, events = _fake_ldn()
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    network = SimpleNamespace(
        address=b"\xaa\xbb\xcc\xdd\xee\xff",
        local_communication_id=0x0100F43008C44000,
        scene_id=1,
        ssid=b"fresh identity!",
    )

    class Scanner:
        def __init__(self, _keys, _monitor):
            pass

        async def receive(self):
            events.append("receive")
            return network

    class Scope:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    module.Scanner = Scanner
    module.trio.move_on_after = lambda _seconds: Scope()

    result = asyncio.run(ldn_single.scan(
        module,
        {},
        phyname="phy0",
        ifname="wlx001122334455",
        channels=(1, 6, 11),
        stop_when=lambda item: item is network,
        preferred_channel=6,
    ))

    assert result == [network]
    assert [event for event in events if event[0] == "channel"] == [
        ("channel", 1), ("channel", 1)
    ]
    assert events.count("receive") == 1
    assert netlink.interface_type == nl.NL80211_IFTYPE_STATION
    commands = [event[1] for event in events if event[0] == "request"]
    assert nl.NL80211_CMD_NEW_INTERFACE not in commands
    assert nl.NL80211_CMD_DEL_INTERFACE not in commands


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_targeted_scan_awaits_match_hook_outside_dwell_but_inside_monitor(
    monkeypatch, outcome,
) -> None:
    module, nl, netlink, events = _fake_ldn()
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    network = SimpleNamespace(
        address=b"\xaa\xbb\xcc\xdd\xee\xff",
        ssid=b"fresh identity!",
    )
    scope_active = False

    class SyntheticCancellation(BaseException):
        pass

    class Scanner:
        def __init__(self, _keys, _monitor):
            pass

        async def receive(self):
            events.append("receive-match")
            return network

    class Scope:
        def __enter__(self):
            nonlocal scope_active
            scope_active = True
            events.append("dwell-enter")
            return self

        def __exit__(self, *_args):
            nonlocal scope_active
            scope_active = False
            events.append("dwell-exit")
            # A real move_on_after suppresses only its own cancellation.  The
            # hook is deliberately awaited after this scope has exited.
            return False

    async def on_match(candidate):
        assert candidate is network
        assert not scope_active
        assert netlink.interface_type == nl.NL80211_IFTYPE_MONITOR
        events.append("match-hook")
        if outcome == "failure":
            raise RuntimeError("synthetic match hook failure")
        if outcome == "cancel":
            raise SyntheticCancellation()

    module.Scanner = Scanner
    module.trio.move_on_after = lambda _seconds: Scope()

    async def exercise():
        return await ldn_single.scan(
            module,
            {},
            phyname="phy0",
            ifname="wlx001122334455",
            channels=(6,),
            stop_when=lambda item: item is network,
            on_match=on_match,
        )

    if outcome == "success":
        assert asyncio.run(exercise()) == [network]
    else:
        expected = RuntimeError if outcome == "failure" else SyntheticCancellation
        with pytest.raises(expected):
            asyncio.run(exercise())
    assert events.index("dwell-exit") < events.index("match-hook")
    assert events.index("match-hook") < events.index("socket-close")
    assert netlink.interface_type == nl.NL80211_IFTYPE_STATION


def test_scan_rejects_match_hook_without_predicate(monkeypatch) -> None:
    module, _nl, _netlink, _events = _fake_ldn()
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)

    async def hook(_network):
        pass

    with pytest.raises(ValueError, match="on_match requires stop_when"):
        asyncio.run(ldn_single.scan(
            module,
            {},
            phyname="phy0",
            ifname="wlx001122334455",
            on_match=hook,
        ))


def test_targeted_scan_uses_preferred_tail_after_balanced_channels(
    monkeypatch,
) -> None:
    module, _nl, _netlink, events = _fake_ldn()
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    network = SimpleNamespace(
        address=b"\xaa\xbb\xcc\xdd\xee\xff",
        ssid=b"preferred channel",
    )

    class DwellExpired(Exception):
        pass

    class Scanner:
        def __init__(self, _keys, _monitor):
            pass

        async def receive(self):
            channels = [
                event[1] for event in events
                if isinstance(event, tuple) and event[0] == "channel"
            ]
            if channels[-1] == 6:
                return network
            raise DwellExpired

    class Scope:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, _exc, _traceback):
            return exc_type is DwellExpired

    windows = []
    module.Scanner = Scanner
    module.trio.move_on_after = lambda seconds: (
        windows.append(seconds) or Scope()
    )

    result = asyncio.run(ldn_single.scan(
        module,
        {},
        phyname="phy0",
        ifname="wlx001122334455",
        channels=(1, 36),
        dwell_time=0.5,
        stop_when=lambda item: item is network,
        preferred_channel=6,
        preferred_dwell_time=6.0,
    ))

    assert result == [network]
    assert [
        event for event in events
        if isinstance(event, tuple) and event[0] == "channel"
    ] == [("channel", 1), ("channel", 1), ("channel", 36), ("channel", 6)]
    assert windows == [
        ldn_single._STATION_DOWN_TIMEOUT_SECONDS,
        0.5,
        0.5,
        6.0,
        ldn_single._INTERFACE_SOCKET_CLOSE_TIMEOUT_SECONDS,
        ldn_single._INTERFACE_TYPE_RESTORE_TIMEOUT_SECONDS,
        ldn_single._STATION_DOWN_TIMEOUT_SECONDS,
    ]


def test_targeted_scan_can_prioritize_previously_detected_channel(
    monkeypatch,
) -> None:
    module, _nl, _netlink, events = _fake_ldn()
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    network = SimpleNamespace(
        address=b"\xaa\xbb\xcc\xdd\xee\xff",
        ssid=b"retry identity!!",
    )

    class Scanner:
        def __init__(self, _keys, _monitor):
            pass

        async def receive(self):
            return network

    class Scope:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    windows = []
    module.Scanner = Scanner
    module.trio.move_on_after = lambda seconds: (
        windows.append(seconds) or Scope()
    )

    result = asyncio.run(ldn_single.scan(
        module,
        {},
        phyname="phy0",
        ifname="wlx001122334455",
        channels=(1, 36),
        stop_when=lambda item: item is network,
        preferred_channel=44,
        preferred_dwell_time=6.0,
        preferred_first=True,
    ))

    assert result == [network]
    assert [
        event for event in events
        if isinstance(event, tuple) and event[0] == "channel"
    ] == [("channel", 44), ("channel", 44)]
    assert windows == [
        ldn_single._STATION_DOWN_TIMEOUT_SECONDS,
        6.0,
        ldn_single._INTERFACE_SOCKET_CLOSE_TIMEOUT_SECONDS,
        ldn_single._INTERFACE_TYPE_RESTORE_TIMEOUT_SECONDS,
        ldn_single._STATION_DOWN_TIMEOUT_SECONDS,
    ]


def test_targeted_scan_replaces_rotating_identity_from_same_bssid(
    monkeypatch,
) -> None:
    module, _nl, _netlink, events = _fake_ldn()
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    address = b"\xaa\xbb\xcc\xdd\xee\xff"
    old = SimpleNamespace(address=address, ssid=b"old identity!!!!")
    fresh = SimpleNamespace(address=address, ssid=b"new identity!!!!")

    class DwellExpired(Exception):
        pass

    class Scanner:
        def __init__(self, _keys, _monitor):
            self.networks = iter((old, fresh))

        async def receive(self):
            events.append("receive")
            try:
                return next(self.networks)
            except StopIteration:
                raise DwellExpired from None

    class Scope:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, _exc, _traceback):
            return exc_type is DwellExpired

    module.Scanner = Scanner
    module.trio.move_on_after = lambda _seconds: Scope()

    result = asyncio.run(ldn_single.scan(
        module,
        {},
        phyname="phy0",
        ifname="wlx001122334455",
        channels=(6,),
        stop_when=lambda _item: False,
    ))

    assert result == [fresh]
    assert [event for event in events if event[0] == "channel"] == [
        ("channel", 6), ("channel", 6)
    ]


def test_targeted_scan_waits_for_first_rotated_identity_from_same_bssid(
    monkeypatch,
) -> None:
    module, _nl, _netlink, events = _fake_ldn()
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    address = b"\xaa\xbb\xcc\xdd\xee\xff"
    failed = SimpleNamespace(address=address, ssid=b"failed identity!")
    rotated = SimpleNamespace(address=address, ssid=b"fresh identity!!")

    class Scanner:
        def __init__(self, _keys, _monitor):
            self.networks = iter((failed, rotated))

        async def receive(self):
            network = next(self.networks)
            events.append(("receive", network.ssid))
            return network

    class Scope:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    module.Scanner = Scanner
    module.trio.move_on_after = lambda _seconds: Scope()

    result = asyncio.run(ldn_single.scan(
        module,
        {},
        phyname="phy0",
        ifname="wlx001122334455",
        channels=(6,),
        stop_when=lambda item: item.ssid != failed.ssid,
    ))

    assert result == [rotated]
    assert [event for event in events if event[0] == "receive"] == [
        ("receive", failed.ssid),
        ("receive", rotated.ssid),
    ]
    assert [event for event in events if event[0] == "channel"] == [
        ("channel", 6), ("channel", 6)
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"preferred_channel": 149}, "Invalid preferred channel"),
        ({"preferred_dwell_time": -0.1}, "preferred dwell time"),
    ],
)
def test_scan_rejects_invalid_preferred_window(monkeypatch, kwargs, message) -> None:
    module, _nl, _netlink, _events = _fake_ldn()
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)

    with pytest.raises(ValueError, match=message):
        asyncio.run(ldn_single.scan(
            module,
            {},
            phyname="phy0",
            ifname="wlx001122334455",
            **kwargs,
        ))


@pytest.mark.parametrize("failure", ["constructor", "activate", "close"])
def test_scan_restores_station_when_monitor_setup_fails(monkeypatch, failure) -> None:
    module, nl, netlink, events = _fake_ldn(monitor_error=failure)
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)

    with pytest.raises(RuntimeError, match="monitor"):
        asyncio.run(ldn_single.scan(
            module, {}, phyname="phy0", ifname="wlx001122334455"
        ))

    assert netlink.interface_type == nl.NL80211_IFTYPE_STATION
    commands = [event[1] for event in events if event[0] == "request"]
    assert nl.NL80211_CMD_NEW_INTERFACE not in commands
    assert nl.NL80211_CMD_DEL_INTERFACE not in commands


def test_connect_cancellation_still_disconnects_and_brings_interface_down(
    monkeypatch,
) -> None:
    module, nl, netlink, events = _fake_ldn()
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    network = SimpleNamespace(
        protocol=3,
        security_mode=1,
        server_random=bytes(16),
        ssid=bytes(16),
        channel=6,
    )
    param = SimpleNamespace(
        client_random=None,
        check=lambda: None,
        network=network,
        keys={},
        password=b"password",
        override_advertise_key=None,
        override_data_key=None,
        override_challenge_key=None,
        phyname="phy0",
    )

    async def cancelled_session():
        async with ldn_single.connect(
            module, param, ifname="wlx001122334455"
        ):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled_session())

    commands = [event[1] for event in events if event[0] == "request"]
    assert nl.NL80211_CMD_DISCONNECT in commands
    assert nl.NL80211_CMD_NEW_INTERFACE not in commands
    assert netlink.interface_type == nl.NL80211_IFTYPE_STATION
    assert events[-1][0] == "link"


def test_connect_bounds_missing_disconnect_ack_and_still_brings_interface_down(
    monkeypatch,
) -> None:
    module, nl, netlink, events = _fake_ldn()
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    logs: list[str] = []
    network = SimpleNamespace(
        protocol=3,
        security_mode=1,
        server_random=bytes(16),
        ssid=bytes(16),
        channel=6,
    )
    param = SimpleNamespace(
        client_random=None,
        check=lambda: None,
        network=network,
        keys={},
        password=b"password",
        override_advertise_key=None,
        override_data_key=None,
        override_challenge_key=None,
        phyname="phy0",
    )

    class SyntheticDeadline(BaseException):
        pass

    class DeadlineScope(_CancelScope):
        def __exit__(self, exc_type, _exc, _traceback):
            return exc_type is SyntheticDeadline

    original_request = netlink.request

    async def missing_disconnect_ack(command, attributes):
        if command == nl.NL80211_CMD_DISCONNECT:
            events.append(("request", command, dict(attributes)))
            raise SyntheticDeadline
        return await original_request(command, attributes)

    netlink.request = missing_disconnect_ack
    module.trio.move_on_after = lambda seconds: (
        events.append(("disconnect-deadline", seconds)) or DeadlineScope()
    )

    async def cancelled_session():
        async with ldn_single.connect(
            module,
            param,
            ifname="wlx001122334455",
            log=logs.append,
        ):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled_session())

    assert (
        "disconnect-deadline",
        ldn_single._STATION_DISCONNECT_TIMEOUT_SECONDS,
    ) in events
    assert any("DISCONNECT acknowledgement timed out" in item for item in logs)
    commands = [event[1] for event in events if event[0] == "request"]
    assert commands.count(nl.NL80211_CMD_DISCONNECT) == 1
    assert events[-1][0] == "link"


def test_connect_real_trio_times_out_hung_disconnect_once(monkeypatch) -> None:
    trio = pytest.importorskip("trio")
    module, nl, netlink, events = _fake_ldn()
    module.trio = trio
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    logs: list[str] = []
    network = SimpleNamespace(
        protocol=3,
        security_mode=1,
        server_random=bytes(16),
        ssid=bytes(16),
        channel=6,
    )
    param = SimpleNamespace(
        client_random=None,
        check=lambda: None,
        network=network,
        keys={},
        password=b"password",
        override_advertise_key=None,
        override_data_key=None,
        override_challenge_key=None,
        phyname="phy0",
    )
    original_request = netlink.request

    async def hung_disconnect(command, attributes):
        if command == nl.NL80211_CMD_DISCONNECT:
            events.append(("request", command, dict(attributes)))
            await trio.sleep_forever()
        return await original_request(command, attributes)

    netlink.request = hung_disconnect

    async def exercise():
        with trio.fail_after(1.5):
            async with ldn_single.connect(
                module,
                param,
                ifname="wlx001122334455",
                log=logs.append,
            ):
                pass

    trio.run(exercise)

    commands = [event[1] for event in events if event[0] == "request"]
    assert commands.count(nl.NL80211_CMD_DISCONNECT) == 1
    assert any("DISCONNECT acknowledgement timed out" in item for item in logs)
    assert events[-1][0] == "link"


def test_connect_real_trio_bounds_hung_final_down(monkeypatch) -> None:
    trio = pytest.importorskip("trio")
    module, nl, _netlink, events = _fake_ldn()
    module.trio = trio
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    network = SimpleNamespace(
        protocol=3,
        security_mode=1,
        server_random=bytes(16),
        ssid=bytes(16),
        channel=6,
    )
    param = SimpleNamespace(
        client_random=None,
        check=lambda: None,
        network=network,
        keys={},
        password=b"password",
        override_advertise_key=None,
        override_data_key=None,
        override_challenge_key=None,
        phyname="phy0",
    )
    original_create_factory = module.wlan.create_factory
    active_factory: dict[str, object] = {}

    @asynccontextmanager
    async def observed_factory():
        async with original_create_factory() as factory:
            active_factory["value"] = factory
            yield factory

    module.wlan.create_factory = observed_factory

    async def exercise():
        with trio.fail_after(1.5):
            async with ldn_single.connect(
                module,
                param,
                ifname="wlx001122334455",
            ):
                factory = active_factory["value"]

                async def hung_down(*_args):
                    await trio.sleep_forever()

                factory._router.update_link = hung_down

    with pytest.raises(RuntimeError, match="timed out.*station DOWN"):
        trio.run(exercise)

    commands = [event[1] for event in events if event[0] == "request"]
    assert commands.count(nl.NL80211_CMD_DISCONNECT) == 1


def test_connect_real_trio_bounds_hung_station_socket_close(monkeypatch) -> None:
    trio = pytest.importorskip("trio")
    module, nl, _netlink, events = _fake_ldn()
    module.trio = trio
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    network = SimpleNamespace(
        protocol=3,
        security_mode=1,
        server_random=bytes(16),
        ssid=bytes(16),
        channel=6,
    )
    param = SimpleNamespace(
        client_random=None,
        check=lambda: None,
        network=network,
        keys={},
        password=b"password",
        override_advertise_key=None,
        override_data_key=None,
        override_challenge_key=None,
        phyname="phy0",
    )
    station_type = module.wlan.Station

    class HungSocket:
        async def aclose(self):
            events.append("hung-station-socket-close")
            await trio.sleep_forever()

    class HungSocketStation(station_type):
        def __init__(self, *args):
            super().__init__(*args)
            self._socket = HungSocket()

    module.wlan.Station = HungSocketStation

    async def exercise():
        with trio.fail_after(1.0):
            async with ldn_single.connect(
                module,
                param,
                ifname="wlx001122334455",
            ):
                pass

    with pytest.raises(RuntimeError, match="closing the station socket"):
        trio.run(exercise)

    assert "hung-station-socket-close" in events
    assert events[-1][0] == "link"
    commands = [event[1] for event in events if event[0] == "request"]
    assert commands.count(nl.NL80211_CMD_DISCONNECT) == 1


def test_connect_rejects_invalid_participant_timeout_before_radio_use() -> None:
    module, _nl, _netlink, events = _fake_ldn()
    network = SimpleNamespace(
        protocol=3,
        security_mode=1,
        server_random=bytes(16),
        ssid=bytes(16),
        channel=6,
    )
    param = SimpleNamespace(
        client_random=None,
        check=lambda: None,
        network=network,
        keys={},
        password=b"password",
        override_advertise_key=None,
        override_data_key=None,
        override_challenge_key=None,
        phyname="phy0",
    )

    async def invalid_session():
        async with ldn_single.connect(
            module,
            param,
            ifname="wlx001122334455",
            participant_timeout=5.01,
        ):
            pytest.fail("invalid timeout reached the radio")

    with pytest.raises(ValueError, match="participant wait timeout"):
        asyncio.run(invalid_session())
    assert events == []


def test_single_interface_participant_wait_uses_bounded_validated_advertisements(
) -> None:
    events: list[object] = []
    logs: list[str] = []
    local_mac = b"\x00\xc0\xca\xba\xb5\x63"
    host = SimpleNamespace(
        ip_address="169.254.42.1",
        mac_address=b"\xbc\x89\xa6\xcc\x23\x55",
        connected=True,
    )
    empty = SimpleNamespace(
        ip_address="0.0.0.0", mac_address=bytes(6), connected=False
    )
    assigned = SimpleNamespace(
        ip_address="169.254.42.2",
        mac_address=local_mac,
        connected=True,
    )
    networks = [
        SimpleNamespace(participants=[host, empty]),
        SimpleNamespace(participants=[host, assigned, empty]),
    ]

    class AdvertisementQueue:
        async def get(self):
            events.append("advertisement")
            return networks.pop(0)

    class Interface:
        async def set_authorized(self):
            events.append("authorized")

        def address(self):
            return local_mac

        async def add_address(self, local, broadcast):
            events.append(("address", local, broadcast))

        async def add_neighbor(self, ip_address, mac_address):
            events.append(("neighbor", ip_address, mac_address))

    class MoveOnAfter:
        def __init__(self, seconds):
            assert seconds == 5.0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    station = SimpleNamespace(
        _interface=Interface(),
        _advertisements=AdvertisementQueue(),
        _network=None,
        _network_id=0,
        _participant_id=0,
    )
    ldn_module = SimpleNamespace(
        trio=SimpleNamespace(move_on_after=MoveOnAfter)
    )

    asyncio.run(
        ldn_single._initialize_single_interface_station(
            station, ldn_module, timeout=5.0, log=logs.append
        )
    )

    assert station._network_id == 42
    assert station._participant_id == 1
    assert events == [
        "authorized",
        "advertisement",
        "advertisement",
        ("address", "169.254.42.2", "169.254.42.255"),
        ("neighbor", "169.254.42.1", host.mac_address),
        ("neighbor", "169.254.42.2", local_mac),
    ]
    assert any(
        "after 2 decoded identity-matching host advertisement(s)" in message
        for message in logs
    )


def test_single_interface_participant_wait_reports_zero_valid_advertisements(
) -> None:
    logs: list[str] = []

    class WaitExpired(Exception):
        pass

    class AdvertisementQueue:
        async def get(self):
            raise WaitExpired

    class Interface:
        async def set_authorized(self):
            pass

        def address(self):
            return b"\x00\xc0\xca\xba\xb5\x63"

    class MoveOnAfter:
        def __init__(self, seconds):
            assert seconds == 5.0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, _exc, _traceback):
            return exc_type is WaitExpired

    station = SimpleNamespace(
        _interface=Interface(),
        _advertisements=AdvertisementQueue(),
    )
    ldn_module = SimpleNamespace(
        trio=SimpleNamespace(move_on_after=MoveOnAfter)
    )

    with pytest.raises(ConnectionError, match="Failed to obtain IP address"):
        asyncio.run(
            ldn_single._initialize_single_interface_station(
                station, ldn_module, timeout=5.0, log=logs.append
            )
        )

    assert any(
        "receiving 0 decoded identity-matching host advertisement(s)" in message
        for message in logs
    )


@pytest.mark.parametrize(
    ("custom_control_port", "control_port_over_nl80211", "missing"),
    [
        (False, True, "custom control-port EtherType 0x88B7"),
        (True, False, "CONTROL_PORT_OVER_NL80211"),
    ],
)
def test_connect_refuses_missing_wiphy_control_port_capability(
    monkeypatch,
    custom_control_port,
    control_port_over_nl80211,
    missing,
) -> None:
    module, nl, netlink, events = _fake_ldn(
        custom_control_port=custom_control_port,
        control_port_over_nl80211=control_port_over_nl80211,
    )
    monkeypatch.setattr(ldn_single, "_interface_driver", lambda _name: "88xxau")
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    network = SimpleNamespace(
        protocol=3,
        security_mode=1,
        server_random=bytes(16),
        ssid=bytes(16),
        channel=6,
    )
    param = SimpleNamespace(
        client_random=None,
        check=lambda: None,
        network=network,
        keys={},
        password=b"password",
        override_advertise_key=None,
        override_data_key=None,
        override_challenge_key=None,
        phyname="phy0",
    )

    async def rejected_session():
        async with ldn_single.connect(
            module, param, ifname="wlx001122334455"
        ):
            pytest.fail("legacy driver must not enter an LDN session")

    with pytest.raises(
        ldn_single.UnsupportedLDNControlPortDriverError,
        match=missing,
    ):
        asyncio.run(rejected_session())

    commands = [event[1] for event in events if event[0] == "request"]
    assert commands == [nl.NL80211_CMD_GET_WIPHY]
    assert "station-connect" not in events
    assert netlink.interface_type == nl.NL80211_IFTYPE_STATION


def test_connect_accepts_wiphy_with_both_control_port_capabilities(
    monkeypatch,
) -> None:
    module, nl, _netlink, events = _fake_ldn(
        custom_control_port=True,
        control_port_over_nl80211=True,
    )
    # Driver names are diagnostic only; advertised capabilities decide.
    monkeypatch.setattr(ldn_single, "_interface_driver", lambda _name: "88xxau")
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    network = SimpleNamespace(
        protocol=3,
        security_mode=1,
        server_random=bytes(16),
        ssid=bytes(16),
        channel=6,
        address=b"\xaa\xbb\xcc\xdd\xee\xff",
    )
    param = SimpleNamespace(
        client_random=None,
        check=lambda: None,
        network=network,
        keys={},
        password=b"password",
        override_advertise_key=None,
        override_data_key=None,
        override_challenge_key=None,
        phyname="phy0",
    )

    async def capable_session():
        async with ldn_single.connect(
            module, param, ifname="wlx001122334455"
        ):
            events.append("session-body")

    asyncio.run(capable_session())

    assert "station-connect" in events
    assert "ldn-start" in events
    assert "session-body" in events
    connect_requests = [
        event[2]
        for event in events
        if event[:2] == ("request", nl.NL80211_CMD_CONNECT)
    ]
    assert connect_requests == [{
        99: b"connect",
        nl.NL80211_ATTR_MAC: network.address,
    }]


def test_connect_ignores_unrelated_nl80211_connect_events(monkeypatch) -> None:
    module, nl, netlink, events = _fake_ldn(connect_messages=[])
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    target = b"\xaa\xbb\xcc\xdd\xee\xff"
    netlink.connect_messages = [
        SimpleNamespace(
            type=nl.NL80211_CMD_CONNECT,
            attributes={
                nl.NL80211_ATTR_IFINDEX: 99,
                nl.NL80211_ATTR_MAC: target,
            },
        ),
        SimpleNamespace(
            type=nl.NL80211_CMD_CONNECT,
            attributes={
                nl.NL80211_ATTR_IFINDEX: 7,
                nl.NL80211_ATTR_MAC: b"\x10\x20\x30\x40\x50\x60",
            },
        ),
        SimpleNamespace(
            type=nl.NL80211_CMD_CONNECT,
            attributes={
                nl.NL80211_ATTR_IFINDEX: 7,
                nl.NL80211_ATTR_MAC: target,
            },
        ),
    ]
    network = SimpleNamespace(
        protocol=3,
        security_mode=1,
        server_random=bytes(16),
        ssid=bytes(16),
        channel=6,
        address=target,
    )
    param = SimpleNamespace(
        client_random=None,
        check=lambda: None,
        network=network,
        keys={},
        password=b"password",
        override_advertise_key=None,
        override_data_key=None,
        override_challenge_key=None,
        phyname="phy0",
    )
    logs = []

    async def filtered_session():
        async with ldn_single.connect(
            module, param, ifname="wlx001122334455", log=logs.append
        ):
            pass

    asyncio.run(filtered_session())

    accepted = [
        event[1]
        for event in events
        if isinstance(event, tuple) and event[0] == "station-connect-event"
    ]
    assert accepted == [{
        nl.NL80211_ATTR_IFINDEX: 7,
        nl.NL80211_ATTR_MAC: target,
    }]
    assert sum("ignored unrelated" in message for message in logs) == 2
    assert sum("CONNECT outcome" in message for message in logs) == 1


def test_connect_primes_hidden_bss_with_bounded_directed_scan(monkeypatch) -> None:
    module, _nl, _netlink, events = _fake_ldn()
    module.wlan.Channels = {6: 2437}
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    scan_calls = []
    network = SimpleNamespace(
        protocol=3,
        security_mode=1,
        server_random=bytes(16),
        ssid=bytes.fromhex("00112233445566778899aabbccddeeff"),
        channel=6,
    )

    def fake_run(command, **kwargs):
        scan_calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=f"BSS 00:11:22:33:44:55\n\tSSID: {network.ssid.hex()}\n",
        )

    monkeypatch.setattr(ldn_single.subprocess, "run", fake_run)
    logs = []
    param = SimpleNamespace(
        client_random=None,
        check=lambda: None,
        network=network,
        keys={},
        password=b"password",
        override_advertise_key=None,
        override_data_key=None,
        override_challenge_key=None,
        phyname="phy0",
    )

    async def capable_session():
        async with ldn_single.connect(
            module, param, ifname="wlx001122334455", log=logs.append
        ):
            events.append("session-body")

    asyncio.run(capable_session())

    assert scan_calls == [
        (
            [
                "iw", "dev", "wlx001122334455", "scan",
                "freq", "2437", "duration", "3000",
                "duration-mandatory", "ssid", network.ssid.hex(),
            ],
            {
                "check": False,
                "stdout": ldn_single.subprocess.PIPE,
                "stderr": ldn_single.subprocess.DEVNULL,
                "text": True,
                "timeout": 8,
            },
        )
    ]
    assert events.index("station-up") < events.index("station-connect")
    assert "[live] directed LDN BSS scan found the advertised SSID" in logs


def test_connect_uses_exact_iw_feature_when_get_wiphy_omits_ext_features(
    monkeypatch,
) -> None:
    module, _nl, _netlink, events = _fake_ldn(
        custom_control_port=True,
        include_ext_features=False,
    )
    iw_calls = []

    def fake_run(command, **kwargs):
        iw_calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "Supported extended features:\n"
                "\t* [ CONTROL_PORT_OVER_NL80211 ]: supported\n"
            ),
        )

    monkeypatch.setattr(ldn_single.subprocess, "run", fake_run)
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    network = SimpleNamespace(
        protocol=3,
        security_mode=1,
        server_random=bytes(16),
        ssid=bytes(16),
        channel=6,
    )
    param = SimpleNamespace(
        client_random=None,
        check=lambda: None,
        network=network,
        keys={},
        password=b"password",
        override_advertise_key=None,
        override_data_key=None,
        override_challenge_key=None,
        phyname="phy11",
    )

    # The fake factory normally represents phy0; permit this test's selected
    # Wiphy while retaining its stable index.
    async def get_wiphy_index(_name):
        return 9

    original_create_factory = module.wlan.create_factory

    @asynccontextmanager
    async def create_factory():
        async with original_create_factory() as factory:
            factory._get_wiphy_index = get_wiphy_index
            yield factory

    module.wlan.create_factory = create_factory

    async def capable_session():
        async with ldn_single.connect(
            module, param, ifname="wlx001122334455"
        ):
            events.append("session-body")

    asyncio.run(capable_session())

    assert iw_calls == [
        (
            ["iw", "phy", "phy11", "info"],
            {
                "check": False,
                "stdout": ldn_single.subprocess.PIPE,
                "stderr": ldn_single.subprocess.DEVNULL,
                "text": True,
                "timeout": 5,
            },
        )
    ]
    assert "station-connect" in events
    assert "session-body" in events


@pytest.mark.parametrize(
    "output",
    [
        "Supported extended features:\n",
        "\t* [ CONTROL_PORT_OVER_NL80211_EXTRA ]\n",
        "\t* prefix [ CONTROL_PORT_OVER_NL80211 ]\n",
    ],
)
def test_iw_feature_fallback_requires_exact_token(monkeypatch, output) -> None:
    monkeypatch.setattr(
        ldn_single.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=output
        ),
    )

    assert ldn_single._iw_has_control_port_over_nl80211("phy11") is False


def test_connect_fails_closed_when_iw_cannot_read_missing_ext_features(
    monkeypatch,
) -> None:
    module, nl, netlink, events = _fake_ldn(
        custom_control_port=True,
        include_ext_features=False,
    )
    monkeypatch.setattr(ldn_single, "_interface_driver", lambda _name: "88xxau")
    monkeypatch.setattr(
        ldn_single.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    monkeypatch.setattr(ldn_single.socket, "if_nametoindex", lambda _name: 7)
    network = SimpleNamespace(
        protocol=3,
        security_mode=1,
        server_random=bytes(16),
        ssid=bytes(16),
        channel=6,
    )
    param = SimpleNamespace(
        client_random=None,
        check=lambda: None,
        network=network,
        keys={},
        password=b"password",
        override_advertise_key=None,
        override_data_key=None,
        override_challenge_key=None,
        phyname="phy0",
    )

    async def rejected_session():
        async with ldn_single.connect(
            module, param, ifname="wlx001122334455"
        ):
            pytest.fail("an unreadable capability must fail closed")

    with pytest.raises(
        ldn_single.UnsupportedLDNControlPortDriverError,
        match="CONTROL_PORT_OVER_NL80211",
    ):
        asyncio.run(rejected_session())

    commands = [event[1] for event in events if event[0] == "request"]
    assert commands == [nl.NL80211_CMD_GET_WIPHY]
    assert "station-connect" not in events
    assert netlink.interface_type == nl.NL80211_IFTYPE_STATION
