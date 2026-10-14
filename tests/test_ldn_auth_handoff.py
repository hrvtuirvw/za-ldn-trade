"""Auth/assignment event-order regression using the installed ldn lifecycle.

Synthetic codecs, real upstream auth retry loop and NetworkInfo identity gates.
No key loading, hardware construction or packet transmission.
"""
from contextlib import asynccontextmanager
from types import FunctionType, SimpleNamespace as NS

import sys
import pytest

if not sys.platform.startswith("linux") or sys.version_info < (3, 12):
    pytest.skip("Linux live-backend regressions require Python 3.12+", allow_module_level=True)
ldn = pytest.importorskip("ldn", reason="install the Linux .[live,test] dependencies")
import trio
from trio.testing import MockClock

from zasim.ldn import ldn_single


def run_case(events, *, fixed=True, deadline=10, process_delay=0, through_connect=False,
             monkeypatch=None):
    host = ldn.wlan.MACAddress(b"\x02\x00\x00\x00\x00\x01")
    local = ldn.wlan.MACAddress(b"\x02\x00\x00\x00\x00\x02")
    other = ldn.wlan.MACAddress(b"\x02\x00\x00\x00\x00\x03")
    calls, logs, stations = [], [], []

    class AuthFrame:
        def __init__(self, *_):
            pass

        def encode(self):
            return b"synthetic-request"

    class Advertisement:
        def __init__(self, *_):
            pass

        def decode(self, data):
            calls.append(("decode", data))
            if data == b"invalid":
                raise ValueError("synthetic invalid advertisement")
            self.kind = data.decode("ascii")

    class Network(ldn.NetworkInfo):
        def __init__(self, protocol=3):
            super().__init__(protocol)
            self.address, self.channel, self.band = host, 6, ldn.ChannelBands[6]
            self.security_mode = ldn.SECURITY_MODE_DEBUG
            self.participants = [NS(mac_address=host, ip_address="169.254.1.1", connected=True)]

        def parse_advertisement(self, frame):
            if frame.kind == "rotated":
                self.ssid = b"new-room"
            if frame.kind != "unassigned":
                self.participants.append(NS(mac_address=local, ip_address="169.254.1.2",
                                            connected=frame.kind != "disconnected"))

    class Interface:
        cursor = 0

        async def next_event(self):
            if self.cursor >= len(events):
                await trio.sleep_forever()
            at, kind = events[self.cursor]
            await trio.sleep_until(at)
            self.cursor += 1
            if kind in ("auth", "wrong_auth", "rejected_auth"):
                return ldn.wlan.CustomFrameEvent(host, kind.encode())
            if kind == "disassociated":
                return ldn.wlan.DisassociationEvent(host)
            return ldn.wlan.ActionFrameEvent(
                NS(source=other if kind == "other_source" else host,
                   action=kind.encode()), 2437,
            )

        async def send_custom_frame(self, address, data):
            assert address == host and data == b"synthetic-request"
            calls.append(("request", trio.current_time()))
            await trio.lowlevel.checkpoint()

        def address(self):
            return local

        async def set_authorized(self):
            calls.append(("authorized", trio.current_time()))
            await trio.lowlevel.checkpoint()

        async def add_address(self, *_):
            calls.append(("address", trio.current_time()))
            await trio.lowlevel.checkpoint()

        async def add_neighbor(self, *_):
            calls.append(("neighbor", trio.current_time()))
            await trio.lowlevel.checkpoint()

    upstream = ldn.STANetwork._authenticate
    authenticate = FunctionType(upstream.__code__,
                                dict(upstream.__globals__, AuthenticationFrame=AuthFrame))
    module = NS(**dict(vars(ldn), AdvertisementFrame=Advertisement, NetworkInfo=Network))

    class Base(ldn.STANetwork):
        def __init__(self, *args):
            super().__init__(*args)
            stations.append(self)
            self.observation = ldn_single._StationEventObservation()

        async def _authenticate(self):
            await authenticate(self)

        def _check_authentication_response(self, address, data):
            if data == b"rejected_auth":
                raise ldn.AuthenticationError(1)
            accepted = address == host and data == b"auth"
            if accepted:
                calls.append(("accepted", trio.current_time()))
            return accepted

        async def _process_events(self):
            await trio.sleep(process_delay)
            await ldn_single._process_single_interface_events(
                self, module, self.observation, log=logs.append,
            )

        async def _initialize_network(self):
            await ldn_single._initialize_single_interface_station(
                self, module, timeout=5, log=logs.append,
            )

    class Fixed(Base):
        async def _authenticate(self):
            await ldn_single._authenticate_single_interface_station(
                self, module, super()._authenticate, log=logs.append,
            )

    interface = Interface()
    param = NS(network=Network(), keys={}, client_random=bytes(16),
               name=b"synthetic", app_version=0, platform=0, enable_challenge=False)
    factory_exits = []
    if through_connect:
        @asynccontextmanager
        async def connection(*args):
            try:
                yield interface
            finally:
                factory_exits.append("interface")

        @asynccontextmanager
        async def factory(*args):
            try:
                yield NS(connect_network=connection)
            finally:
                factory_exits.append("factory")

        monkeypatch.setattr(ldn_single, "_factory", factory)
        module.STANetwork = Base
        module.KeyDerivation = lambda *a, **kw: None
        param.network.security_mode = ldn.SECURITY_MODE_DEBUG
        param.check = lambda: None
        param.override_advertise_key = param.override_data_key = param.override_challenge_key = None
        param.phyname = "synthetic-no-radio"

    async def run():
        try:
            with trio.move_on_after(deadline) as scope:
                if through_connect:
                    context = ldn_single.connect(module, param, ifname="synthetic-no-radio", log=logs.append)
                else:
                    context = (Fixed if fixed else Base)(interface, param, None).start()
                async with context:
                    calls.append(("assigned", trio.current_time()))
            outcome = "cancelled" if scope.cancelled_caught else "assigned"
        except Exception as exc:
            while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
                exc = exc.exceptions[0]
            outcome = (type(exc).__name__, str(exc))
        assert len(stations) == 1
        assert stations[0]._interface is interface
        assert getattr(stations[0], "_pending_auth_advertisement", None) is None
        return outcome, trio.current_time()

    outcome, elapsed = trio.run(run, clock=MockClock(autojump_threshold=0))
    return NS(outcome=outcome, elapsed=elapsed, calls=calls, logs=logs,
              station=stations[0], factory_exits=factory_exits)


def has(result, name):
    return [value for call, value in result.calls if call == name]


def test_actual_upstream_discards_sole_assignment_fixed_path_consumes_once():
    events = ((.01, "assigned"), (.02, "auth"))
    old, fixed = run_case(events, fixed=False), run_case(events)
    assert old.outcome == ("ConnectionError", "Failed to obtain IP address after joining network (timeout)")
    assert old.elapsed == pytest.approx(5.02)
    assert fixed.outcome == "assigned" and fixed.elapsed == pytest.approx(.02)
    assert has(old, "request") == has(fixed, "request") == [0]
    assert has(old, "decode") == []
    assert has(fixed, "decode") == [b"assigned"]
    assert has(fixed, "address") == [.02]
    assert fixed.station.observation.counts["auth_handoff"] == 1
    assert fixed.calls.index(("accepted", .02)) < fixed.calls.index(("decode", b"assigned"))
    assert fixed.calls.index(("accepted", .02)) < fixed.calls.index(("authorized", .02))


@pytest.mark.parametrize("events,outcome,requests", [
    (((.01, "auth"), (.02, "assigned")), "assigned", 1),
    (((.01, "assigned"), (.015, "other_source"), (.02, "auth")), "assigned", 1),
    (((.01, "other_source"), (.02, "auth")), "timeout", 1),
    (((.01, "unassigned"), (.02, "auth")), "timeout", 1),
    (((.01, "assigned"), (.015, "unassigned"), (.02, "auth")), "timeout", 1),
    (((.01, "assigned"), (.015, "invalid"), (.02, "auth")), "timeout", 1),
    (((.01, "assigned"), (.015, "disconnected"), (.02, "auth")), "timeout", 1),
    (((.01, "auth"), (.02, "disconnected")), "timeout", 1),
    (((.01, "assigned"), (.8, "auth")), "timeout", 2),  # Retry clears old slot.
    (((.01, "assigned"), (.75, "assigned"), (.8, "auth")), "assigned", 2),
])
def test_latest_only_current_attempt_and_active_participant(events, outcome, requests):
    result = run_case(events)
    if outcome == "assigned":
        assert result.outcome == "assigned"
        assert len(has(result, "address")) == 1
    else:
        assert result.outcome == ("ConnectionError", "Failed to obtain IP address after joining network (timeout)")
        assert has(result, "address") == []
    assert len(has(result, "request")) == requests


@pytest.mark.parametrize("last,exception,message", [
    ("rotated", "ConnectionError", "Received incompatible advertisement frame from host"),
    ("disassociated", "ConnectionError", "Station was disassociated"),
    ("rejected_auth", "AuthenticationError", None),
])
def test_failures_preserve_auth_identity_gates_and_restore_interface(last, exception, message):
    result = run_case(((.01, "assigned"), (.015, last), (.02, "auth")))
    assert result.outcome[0] == exception
    if message:
        assert result.outcome[1] == message
    assert has(result, "address") == []
    if last != "rotated":
        assert has(result, "authorized") == has(result, "decode") == []


@pytest.mark.parametrize("events,deadline", [
    (((.01, "assigned"),), 10),
    (((.01, "assigned"), (.02, "wrong_auth")), 10),
    (((.01, "assigned"), (.02, "auth")), .015),
])
def test_no_auth_success_never_releases_advertisement(events, deadline):
    result = run_case(events, deadline=deadline)
    assert result.outcome == ("cancelled" if deadline < 1 else
                              ("ConnectionError", "Authentication timeout (password may be wrong)"))
    assert has(result, "address") == has(result, "decode") == has(result, "authorized") == []
    assert len(has(result, "request")) == (1 if deadline < 1 else 3)


def test_handoff_age_bound_survives_delayed_processor_start():
    result = run_case(((.01, "assigned"), (.02, "auth")), process_delay=.7)
    assert result.outcome[0] == "ConnectionError"
    assert has(result, "decode") == has(result, "address") == []
    assert any("expired=true" in line for line in result.logs)


@pytest.mark.parametrize("deadline", [10, .015, .03])
def test_production_connect_subclass_wires_handoff_and_unwinds_factory(monkeypatch, deadline):
    def forbidden(*args, **kwargs):
        pytest.fail("offline regression attempted radio/key access")

    monkeypatch.setattr(ldn, "load_keys", forbidden)
    monkeypatch.setattr(ldn.wlan.Interface, "__init__", forbidden)
    monkeypatch.setattr(ldn.wlan, "create_factory", forbidden)
    original_auth = ldn.STANetwork._authenticate
    # At .03 this is cancelled during assignment (no buffered advertisement).
    events = ((.01, "assigned"), (.02, "auth")) if deadline != .03 else ((.02, "auth"),)
    result = run_case(events, through_connect=True, monkeypatch=monkeypatch, deadline=deadline)
    assert result.outcome == ("assigned" if deadline == 10 else "cancelled")
    assert result.factory_exits == ["interface", "factory"]
    assert ldn.STANetwork._authenticate is original_auth
    if deadline == 10:
        assert result.station._station_event_observation.counts["auth_handoff"] == 1
        handoff = " ".join(result.logs)
        assert "interface_continuity=true" in handoff
        assert "advertisement_queue_continuity=true" in handoff
        assert "advertisement_queue_depth_before=0" in handoff
        assert "advertisement_queue_depth_after=0" in handoff
    for private in ("synthetic-request", "169.254", "02:00:", "new-room"):
        assert private not in "\n".join(result.logs)


@pytest.mark.parametrize("deadline", [10, .03])
def test_assignment_timeout_emits_payload_free_queue_snapshot(monkeypatch, deadline):
    result = run_case(((.02, "auth"),), through_connect=True,
                      deadline=deadline, monkeypatch=monkeypatch)
    timed_out = deadline == 10
    assert result.outcome == (
        ("ConnectionError", "Failed to obtain IP address after joining network (timeout)")
        if timed_out else "cancelled"
    )
    assert result.elapsed == pytest.approx(5.02 if timed_out else deadline)
    # Outer cancellation is not a completed assignment timeout. Its finally
    # summaries survive, but must not manufacture timeout/queue-snapshot evidence.
    assert any("stage=assignment-exit" in line for line in result.logs)
    assert any("stage=processor-exit" in line for line in result.logs)
    assert any("participant assignment wait expired" in line for line in result.logs) == timed_out
    assert any("LDN assignment queue snapshot depth=0" in line for line in result.logs) == timed_out
    assert any(
        "LDN assignment queue identity continuity=true replaced=false" in line
        for line in result.logs
    ) == timed_out
    assert result.factory_exits == ["interface", "factory"]
    assert has(result, "address") == []
