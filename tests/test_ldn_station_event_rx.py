"""Offline discrimination of missing station RX vs silently rejected adverts."""
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import sys
import pytest

if not sys.platform.startswith("linux") or sys.version_info < (3, 12):
    pytest.skip("Linux live-backend regressions require Python 3.12+", allow_module_level=True)
ldn = pytest.importorskip("ldn", reason="install the Linux .[live,test] dependencies")

from zasim.ldn import ldn_single

trio = pytest.importorskip("trio")
from trio.testing import MockClock

HEADER = bytes.fromhex("7f0022aa04000101")


class ActionEvent:
    def __init__(self, source=b"hostxx", data=b"valid", frequency=2437):
        self.frame = NS(source=source, action=data)
        self.frequency = frequency


class CustomEvent:
    def __init__(self, data=b"disconnect"):
        self.data = data


class DisassociationEvent:
    pass


def fixture(events, *, put_error=False):
    calls = []

    class Interface:
        async def next_event(self):
            await trio.lowlevel.checkpoint()
            if events:
                return events.pop(0)
            await trio.sleep_forever()

    class Advertisement:
        def __init__(self, key, protocol):
            assert key == "synthetic-key" and protocol == 3

        def decode(self, data):
            calls.append(("decode", data))
            data = data.removeprefix(HEADER)
            if data == b"invalid-secret":
                raise ValueError("private decoder exception")
            self.data = data

    class Network:
        def __init__(self, protocol):
            self.protocol = protocol

        def parse_advertisement(self, frame):
            calls.append(("parse", frame.data))
            if frame.data == b"parse-error":
                raise ValueError("private parse exception")
            self.compatible = frame.data != b"rotated"

    class Queue:
        def __init__(self):
            self.items = []

        async def put(self, item):
            if put_error:
                raise RuntimeError("private queue exception")
            await trio.lowlevel.checkpoint()
            self.items.append(item)

    class Disconnect:
        def decode(self, data):
            if data == b"bad-disconnect":
                raise ValueError("private disconnect exception")
            self.reason = 7

    module = NS(
        wlan=NS(ActionFrameEvent=ActionEvent, CustomFrameEvent=CustomEvent,
                DisassociationEvent=DisassociationEvent,
                map_frequency=lambda freq: {2437: 6, 2412: 1}[freq]),
        AdvertisementFrame=Advertisement, NetworkInfo=Network,
        ChannelBands={1: 2, 6: 2}, DisconnectFrame=Disconnect,
        DisconnectEvent=lambda reason: NS(reason=reason),
        DISCONNECT_CONNECTION_LOST=99,
    )
    station = NS(
        _interface=Interface(), _network=NS(address=b"hostxx", protocol=3,
            is_same_network=lambda info: info.compatible),
        _key_derivation="synthetic-key", _advertisements=Queue(), _events=Queue(),
    )
    return module, station, calls


def run_processor(events, *, upstream=False, put_error=False):
    module, station, calls = fixture(list(events), put_error=put_error)
    logs = []
    observation = None if upstream else ldn_single._StationEventObservation()

    async def run():
        with trio.move_on_after(1):
            if upstream:
                # Execute the actual pinned upstream function against the same
                # event/decoder fakes, without importing/starting radio objects.
                import ldn
                from types import FunctionType
                fn = ldn.STANetwork._process_events
                globals_ = dict(fn.__globals__)
                globals_.update(vars(module))
                await FunctionType(fn.__code__, globals_)(station)
            else:
                await ldn_single._process_single_interface_events(
                    station, module, observation, log=logs.append
                )

    error = None
    try:
        trio.run(run, clock=MockClock(autojump_threshold=0))
    except Exception as exc:
        error = (type(exc), str(exc))
    return station, calls, logs, observation, error


@pytest.mark.parametrize("events,counts", [
    ([], {"events": 0, "host_actions": 0, "queued": 0}),
    ([ActionEvent(source=b"otherx")], {"events": 1, "actions": 1,
                                      "other_source": 1, "host_actions": 0}),
    ([ActionEvent(data=b"invalid-secret")], {"host_actions": 1,
                                            "decode_rejected": 1, "queued": 0}),
    ([ActionEvent()], {"host_actions": 1, "host_candidates": 0,
                       "decoded": 1, "queue_started": 1, "queued": 1}),
    ([ActionEvent(data=HEADER + b"valid")], {"host_candidates": 1, "queued": 1}),
    ([ActionEvent(data=HEADER + b"invalid-secret")],
     {"host_candidates": 1, "candidate_decode_rejected": 1, "queue_started": 0}),
    ([ActionEvent(source=b"otherx", data=HEADER + b"valid")],
     {"other_source": 1, "host_candidates": 0}),
    ([ActionEvent(data=b"rotated")], {"decoded": 1, "identity_rejected": 1,
                                     "queued": 0}),
    ([ActionEvent(data=b"parse-error")], {"decoded": 1, "parse_failed": 1,
                                         "queued": 0}),
    ([ActionEvent(frequency=9999)], {"decoded": 1, "parse_failed": 1}),
    ([CustomEvent(), DisassociationEvent(), object()],
     {"events": 3, "custom": 1, "disassociation": 1, "other": 1}),
])
def test_distinguishes_station_rx_loss_from_filter_decode_and_identity(events, counts):
    _, _, logs, observation, _ = run_processor(events)
    assert len(logs) == 1  # no per-frame log spam
    assert "stage=processor-exit started=true" in logs[0]
    for key, value in counts.items():
        assert observation.counts[key] == value
        assert f"{key}={value}" in logs[0].split()
    for private in ("hostxx", "otherx", "secret", "exception", "rotated",
                    "synthetic-key", "parse-error", "9999"):
        assert private not in logs[0]


@pytest.mark.parametrize("events,put_error", [
    ([], False),
    ([ActionEvent(source=b"otherx"), ActionEvent(data=b"invalid-secret"),
      ActionEvent(), CustomEvent(), DisassociationEvent(), object()], False),
    ([ActionEvent(data=b"rotated")], False),
    ([ActionEvent(data=b"parse-error")], False),
    ([ActionEvent(frequency=9999)], False),
    ([CustomEvent(data=b"bad-disconnect")], False),
    ([ActionEvent()], True),
])
def test_preserves_actual_pinned_upstream_filter_queue_and_exception_semantics(events, put_error):
    observed = run_processor(events, put_error=put_error)
    upstream = run_processor(events, upstream=True, put_error=put_error)
    assert observed[1] == upstream[1]  # decoder call order
    assert observed[4] == upstream[4]  # error type and exact upstream message
    for queue in ("_advertisements", "_events"):
        assert [vars(v) for v in getattr(observed[0], queue).items] == [
            vars(v) for v in getattr(upstream[0], queue).items]
    if put_error:
        assert observed[3].counts["queue_started"] == 1
        assert observed[3].counts["queued"] == 0


def test_empty_unstarted_observation_is_not_claimed_as_active_zero_rx():
    observation = ldn_single._StationEventObservation()
    assert "started=false" in observation.summary("assignment-exit")
    with pytest.raises(ValueError):
        observation.summary("private arbitrary label")


@pytest.mark.parametrize("outcome", ["timeout", "assigned", "cancel"])
def test_real_connect_wires_observer_and_flushes_before_cleanup(monkeypatch, outcome):
    logs, lifecycle = [], []
    module, station, _ = fixture([ActionEvent(data=b"invalid-secret")])

    class BaseStation:
        def __init__(self, interface, param, key):
            self.__dict__.update(vars(station))
            self._interface = interface

        @asynccontextmanager
        async def start(self):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(self._process_events)
                try:
                    await self._initialize_network()
                    yield
                finally:
                    nursery.cancel_scope.cancel()

    async def initialize(instance, ldn_module, *, timeout, log):
        assert timeout == 5.0 and ldn_module is module
        await trio.sleep(0.2)
        if outcome == "timeout":
            raise ConnectionError("assignment timeout")
        if outcome == "cancel":
            await trio.sleep_forever()

    @asynccontextmanager
    async def factory(*args):
        class Factory:
            @asynccontextmanager
            async def connect_network(self, *args):
                try:
                    yield station._interface
                finally:
                    lifecycle.append(("cleanup", list(logs)))
        yield Factory()

    monkeypatch.setattr(ldn_single, "_factory", factory)
    monkeypatch.setattr(ldn_single, "_initialize_single_interface_station", initialize)
    module.STANetwork = BaseStation
    module.KeyDerivation = lambda *args, **kwargs: object()
    module.SECURITY_MODE_PROD = 1
    param = NS(client_random=b"not-random", check=lambda: None,
        network=NS(protocol=3, security_mode=0, address=b"hostxx", ssid=b"ssid",
                   channel=6), keys={}, override_advertise_key=None,
        override_data_key=None, override_challenge_key=None, phyname="fake-phy")

    async def run():
        with trio.move_on_after(0.5):
            try:
                async with ldn_single.connect(module, param, ifname="fake", log=logs.append):
                    lifecycle.append(("yield", None))
            except ExceptionGroup as exc:
                assert outcome == "timeout"
                assert all(isinstance(e, ConnectionError) for e in exc.exceptions)

    trio.run(run, clock=MockClock(autojump_threshold=0))
    assert len(logs) == 2
    assert sum("stage=assignment-exit" in line for line in logs) == 1
    assert sum("stage=processor-exit" in line for line in logs) == 1
    assert all("host_actions=1" in line and "decode_rejected=1" in line for line in logs)
    assert lifecycle[-1] == ("cleanup", logs)
    assert any(item[0] == "yield" for item in lifecycle) == (outcome == "assigned")


def test_queue_cancellation_is_not_counted_as_completed_delivery():
    module, station, _ = fixture([ActionEvent(data=HEADER + b"valid")])
    observation = ldn_single._StationEventObservation()
    logs = []

    async def blocked_put(info):
        await trio.sleep_forever()

    station._advertisements.put = blocked_put

    async def run():
        with trio.move_on_after(1):
            await ldn_single._process_single_interface_events(
                station, module, observation, log=logs.append
            )

    trio.run(run, clock=MockClock(autojump_threshold=0))
    assert observation.counts["decoded"] == 1
    assert observation.counts["queue_started"] == 1
    assert observation.counts["queued"] == 0
    assert len(logs) == 1
