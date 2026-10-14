"""Real Trio lifetime regressions; no socket, netlink or radio is opened."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
import sys

import pytest

if not sys.platform.startswith("linux") or sys.version_info < (3, 12):
    pytest.skip("Linux live-backend regressions require Python 3.12+", allow_module_level=True)
ldn = pytest.importorskip("ldn", reason="install the Linux .[live,test] dependencies")

from zasim.ldn import ldn_single


def _matched_value_errors(error):
    # This module is skipped on Python <3.12, but pytest must first parse it.
    # Avoid except* syntax so Python3.10 can reach that explicit skip.
    if isinstance(error, ValueError):
        return (error,)
    matched, remaining = error.split(ValueError)
    if remaining is not None:
        raise error
    return matched.exceptions


@pytest.mark.parametrize("outcome", ["normal", "error", "cancel"])
def test_factory_receiver_outlives_shielded_cleanup(outcome):
    trio = pytest.importorskip("trio")
    events = []

    async def exercise():
        send, receive = trio.open_memory_channel(1)

        async def receiver():
            events.append("receiver-start")
            try:
                async for ack in receive:
                    ack.set()
            finally:
                events.append("receiver-stop")

        async def request(*_args):
            ack = trio.Event()
            await send.send(ack)
            await ack.wait()
            events.append("cleanup-ack")

        @asynccontextmanager
        async def backend():
            # Same lifetime structure as netlink.connect: receivers are
            # children of a nursery established by create_factory.__aenter__.
            async with trio.open_nursery() as nursery:
                nursery.start_soon(receiver)
                try:
                    yield SimpleNamespace(
                        _wlan=SimpleNamespace(request=request),
                        _router=SimpleNamespace(update_link=request),
                    )
                finally:
                    events.append("factory-close")
                    nursery.cancel_scope.cancel()

        module = SimpleNamespace(
            trio=trio, wlan=SimpleNamespace(create_factory=backend)
        )
        try:
            with trio.CancelScope() as cancelled:
                async with ldn_single._factory(module, "synthetic0") as factory:
                    try:
                        await trio.sleep(0)
                        if outcome == "error":
                            raise ValueError("synthetic body failure")
                        if outcome == "cancel":
                            cancelled.cancel()
                            await trio.sleep_forever()
                    finally:
                        # A test-only bound prevents the pre-fix deadlock
                        # from leaving a thread/process behind.
                        with trio.move_on_after(0.1, shield=True):
                            await factory._wlan.request()
                            await factory._router.update_link()
                        events.append("cleanup-finished")
        except (ValueError, ExceptionGroup) as errors:
            _matched_value_errors(errors)
            assert outcome == "error"
        assert events.count("cleanup-ack") == 2, events
        assert events.index("cleanup-finished") < events.index("receiver-stop")
        assert events.count("receiver-stop") == 1
        assert events.count("factory-close") == 1

    trio.run(exercise)


@pytest.mark.parametrize("phase", ["scan", "join"])
def test_pre_ready_worker_stop_keeps_cleanup_receivers_alive(monkeypatch, phase):
    import sys
    import threading
    from zasim.ldn import transport

    trio = pytest.importorskip("trio")
    entered = threading.Event()
    events = []
    module = SimpleNamespace(
        trio=trio, ACCEPT_NONE=0, ConnectNetworkParam=SimpleNamespace,
        load_keys=lambda _path: {},
    )
    live = transport.LiveTransport(
        single_interface=True, require_exact_network=True,
        log=lambda _message: None,
    )
    live._single_interface_active = True
    live._attempt_stop = threading.Event()
    network = SimpleNamespace(
        accept_policy=1, num_participants=1, max_participants=2,
        local_communication_id=live.LOCAL_COMMUNICATION_ID,
        scene_id=live.SCENE_ID, application_data=b"room",
        ssid=b"synthetic-ssid!!", channel=1, app_version=1,
    )

    @asynccontextmanager
    async def backend():
        request_sent = trio.Event()
        ack = trio.Event()

        async def receiver():
            try:
                await request_sent.wait()
                await trio.sleep(0)
                ack.set()
                await trio.sleep_forever()
            finally:
                events.append("receiver-stop")

        async def request():
            request_sent.set()
            await ack.wait()
            events.append("cleanup-ack")

        async with trio.open_nursery() as nursery:
            nursery.start_soon(receiver)
            try:
                yield SimpleNamespace(
                    _wlan=SimpleNamespace(request=request), _router=None,
                )
            finally:
                nursery.cancel_scope.cancel()

    module.wlan = SimpleNamespace(create_factory=backend)

    @asynccontextmanager
    async def cancelled_operation():
        async with ldn_single._factory(module, "synthetic0") as factory:
            try:
                entered.set()
                await trio.sleep_forever()
                yield  # never reached; simulates scan or association awaiting RX
            finally:
                with trio.move_on_after(0.3, shield=True):
                    await factory._wlan.request()
                events.append("cleanup-finished")

    async def scan(*_args, **_kwargs):
        if phase == "scan":
            async with cancelled_operation():
                pass
        return [network]

    @asynccontextmanager
    async def connect(*_args, **_kwargs):
        async with cancelled_operation():
            yield

    monkeypatch.setitem(sys.modules, "ldn", module)
    monkeypatch.setattr(ldn_single, "scan", scan)
    monkeypatch.setattr(ldn_single, "connect", connect)
    worker = threading.Thread(target=live._run_ldn, daemon=True)
    live._thread = worker
    worker.start()
    try:
        assert entered.wait(1.0)
        assert not live._ready.is_set()
    finally:
        live._signal_worker_stop()
        live._join_worker_or_raise()
    assert live._thread is None
    assert not worker.is_alive()
    assert live._err is None
    assert events == ["cleanup-ack", "cleanup-finished", "receiver-stop"]


def test_factory_startup_remains_cancellable():
    trio = pytest.importorskip("trio")
    events = []

    @asynccontextmanager
    async def backend():
        try:
            await trio.sleep_forever()
            yield
        finally:
            events.append("startup-cancelled")

    async def exercise():
        with trio.move_on_after(0.01) as cancel:
            async with ldn_single._keep_factory_receivers_alive(
                SimpleNamespace(create_factory=backend), trio
            ):
                pytest.fail("startup never completed")
        assert cancel.cancelled_caught

    trio.run(exercise)
    assert events == ["startup-cancelled"]


def test_factory_startup_failure_is_propagated():
    trio = pytest.importorskip("trio")
    caught = []

    @asynccontextmanager
    async def backend():
        raise ValueError("synthetic factory startup failure")
        yield

    async def exercise():
        try:
            async with ldn_single._keep_factory_receivers_alive(
                SimpleNamespace(create_factory=backend), trio
            ):
                pytest.fail("startup failed")
        except (ValueError, ExceptionGroup) as errors:
            caught.extend(_matched_value_errors(errors))

    trio.run(exercise)
    assert len(caught) == 1
    assert str(caught[0]) == "synthetic factory startup failure"


def test_non_stopping_worker_still_blocks_radio_teardown(monkeypatch):
    from zasim.ldn import transport

    joined = []
    worker = SimpleNamespace(
        ident=1, join=lambda timeout: joined.append(timeout),
        is_alive=lambda: True,
    )
    live = transport.LiveTransport(log=lambda _message: None)
    live._thread = worker
    token = transport.threading.Event()
    live._attempt_stop = token
    monkeypatch.setattr(
        live, "_teardown_radio",
        lambda: pytest.fail("must not mutate a radio still owned by a worker"),
    )
    with pytest.raises(RuntimeError, match="LDN worker did not stop"):
        live.stop()
    assert joined == [2]
    assert live._thread is worker
    assert live._attempt_stop is token
    assert token.is_set()
