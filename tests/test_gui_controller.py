from __future__ import annotations

from pathlib import Path
import threading
import time

import pytest

from zasim.client import LiveTradeResult
from zasim.control import OperationCancelled
from zasim.gui_controller import (
    ConnectionSettings,
    ControllerState,
    GuiController,
    ProbeRequest,
    TradeRequest,
)


def _trade_request(
    tmp_path: Path,
    *,
    connection: ConnectionSettings | None = None,
    auto_confirm: bool = False,
) -> TradeRequest:
    offer = tmp_path / "offer.epa9"
    offer.write_bytes(b"test offer")
    return TradeRequest(
        connection=connection or ConnectionSettings(),
        offer=offer,
        output=tmp_path / "received.epa9",
        link_code="12345678",
        auto_confirm=auto_confirm,
    )


def _events_by_kind(controller: GuiController) -> dict[str, list[object]]:
    grouped: dict[str, list[object]] = {}
    for event in controller.drain_events():
        grouped.setdefault(event.kind, []).append(event.payload)
    return grouped


def _wait_for(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("timed out waiting for controller state")
        time.sleep(0.001)


def test_connection_settings_reject_windows_direct_and_map_linux_direct() -> None:
    direct = ConnectionSettings(
        backend="direct",
        keys_path="/home/test/.switch/prod.keys",
        phyname="phy7",
        ifname="ldn-test",
        nickname="Linux Player",
    )

    with pytest.raises(ValueError, match="only on Linux"):
        direct.validate(os_name="nt")

    assert direct.live_kwargs(os_name="posix") == {
        "keys_path": "/home/test/.switch/prod.keys",
        "relay": None,
        "phyname": "phy7",
        "ifname": "ldn-test",
        "nickname": "Linux Player",
    }


def test_connection_settings_map_windows_relay_without_keys() -> None:
    relay = ConnectionSettings(
        backend="relay",
        relay_endpoint="127.0.0.1:25000",
        keys_path="must-not-be-forwarded",
        nickname="Windows Player",
    )

    kwargs = relay.live_kwargs(os_name="nt")
    assert kwargs["relay"] == "127.0.0.1:25000"
    assert kwargs["keys_path"] is None

    # Hidden direct-only fields do not make relay mode invalid.
    ConnectionSettings(
        backend="relay", phyname="", ifname="", nickname="Relay"
    ).validate(os_name="nt")


def test_connection_settings_reject_direct_mode_on_macos() -> None:
    with pytest.raises(ValueError, match="only on Linux"):
        ConnectionSettings(backend="direct").validate(
            os_name="posix", sys_platform="darwin"
        )


@pytest.mark.parametrize(
    ("nickname", "message"),
    [("A" * 21, "20 Unicode characters"), ("😀" * 9, "32 UTF-8 bytes")],
)
def test_connection_settings_reject_out_of_range_ldn_nickname(
    nickname: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ConnectionSettings(nickname=nickname).validate(os_name="nt")


@pytest.mark.parametrize(
    ("changes", "error_type", "message"),
    [
        ({"timeout": 0}, ValueError, "trade timeout"),
        ({"timeout": float("nan")}, ValueError, "trade timeout"),
        ({"settle_grace": -1}, ValueError, "settle grace"),
        ({"auto_confirm": 1}, TypeError, "auto_confirm"),
    ],
)
def test_trade_request_rejects_invalid_runtime_values(
    tmp_path: Path,
    changes: dict[str, object],
    error_type: type[Exception],
    message: str,
) -> None:
    request = _trade_request(tmp_path)
    values = {
        "connection": request.connection,
        "offer": request.offer,
        "output": request.output,
        "link_code": request.link_code,
        "timeout": request.timeout,
        "settle_grace": request.settle_grace,
        "auto_confirm": request.auto_confirm,
    }
    values.update(changes)

    with pytest.raises(error_type, match=message):
        TradeRequest(**values).validate(os_name="nt")


def test_trade_request_validates_input_and_output_paths(tmp_path: Path) -> None:
    connection = ConnectionSettings()
    missing = tmp_path / "missing.epa9"
    with pytest.raises(ValueError, match="offer file does not exist"):
        TradeRequest(
            connection, missing, tmp_path / "received.epa9", "12345678"
        ).validate(os_name="nt")

    offer = tmp_path / "offer.epa9"
    offer.write_bytes(b"test offer")
    with pytest.raises(ValueError, match="must be different"):
        TradeRequest(connection, offer, offer, "12345678").validate(os_name="nt")
    with pytest.raises(ValueError, match="output directory"):
        TradeRequest(
            connection,
            offer,
            tmp_path / "missing-dir" / "received.epa9",
            "12345678",
        ).validate(os_name="nt")
    with pytest.raises(ValueError, match="must use .pa9"):
        TradeRequest(
            connection, offer, tmp_path / "received.bin", "12345678"
        ).validate(os_name="nt")


def test_probe_request_validation(tmp_path: Path) -> None:
    connection = ConnectionSettings()
    valid = ProbeRequest(
        connection, tmp_path / "capture.jsonl", "12345678", duration=0.25
    )
    assert valid.validate(os_name="nt") is valid

    with pytest.raises(ValueError, match=".jsonl extension"):
        ProbeRequest(
            connection, tmp_path / "capture.txt", "12345678"
        ).validate(os_name="nt")
    with pytest.raises(ValueError, match="capture duration"):
        ProbeRequest(
            connection,
            tmp_path / "capture.jsonl",
            "12345678",
            duration=0,
        ).validate(os_name="nt")
    with pytest.raises(ValueError, match="capture directory"):
        ProbeRequest(
            connection,
            tmp_path / "missing-dir" / "capture.jsonl",
            "12345678",
        ).validate(os_name="nt")

    with pytest.raises(ValueError, match="8 ASCII digits"):
        ProbeRequest(connection, tmp_path / "capture.jsonl", "１２３４５６７８").validate(
            os_name="nt"
        )


def test_trade_success_maps_request_and_emits_logs_and_result(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    expected = LiveTradeResult(b"received", 1.25, tmp_path / "received.epa9")

    def trade_runner(offer: Path, **kwargs: object) -> LiveTradeResult:
        captured["offer"] = offer
        captured.update(kwargs)
        kwargs["log"]("connected")  # type: ignore[operator]
        return expected

    request = _trade_request(tmp_path, auto_confirm=True)
    controller = GuiController(trade_runner=trade_runner, os_name="nt")
    job_id = controller.start_trade(request)

    assert controller.wait(1)
    assert controller.state is ControllerState.SUCCEEDED
    assert controller.is_busy is False
    assert captured["offer"] == request.offer
    assert captured["output"] == request.output
    assert captured["link_code"] == "12345678"
    assert captured["relay"] == "127.0.0.1:24872"
    assert captured["keys_path"] is None
    assert captured["auto_confirm"] is True
    assert captured["confirmation_callback"] is None
    assert isinstance(captured["cancel_event"], threading.Event)

    events = _events_by_kind(controller)
    assert events["log"] == ["connected"]
    assert events["succeeded"] == [expected]
    assert controller.acknowledge(job_id)
    assert controller.state is ControllerState.IDLE


def test_probe_success_uses_linux_direct_mapping(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    expected = {"output": str(tmp_path / "capture.jsonl"), "packets": 3}

    def probe_runner(**kwargs: object) -> dict[str, int | str]:
        captured.update(kwargs)
        return expected

    connection = ConnectionSettings(
        backend="direct",
        keys_path="/home/test/prod.keys",
        phyname="phy2",
        ifname="ldn2",
    )
    request = ProbeRequest(
        connection, tmp_path / "capture.jsonl", "12345678", duration=1.5
    )
    controller = GuiController(probe_runner=probe_runner, os_name="posix")
    job_id = controller.start_probe(request)

    assert controller.wait(1)
    assert controller.state is ControllerState.SUCCEEDED
    assert captured["keys_path"] == "/home/test/prod.keys"
    assert captured["relay"] is None
    assert captured["phyname"] == "phy2"
    assert captured["ifname"] == "ldn2"
    assert captured["output"] == request.output
    assert captured["duration"] == 1.5
    assert captured["link_code"] == "12345678"
    events = controller.drain_events()
    assert events[-1].job_id == job_id
    assert events[-1].kind == "succeeded"
    assert events[-1].payload == expected


def test_runner_failure_sets_failed_state_and_event(tmp_path: Path) -> None:
    failure = OSError("radio unavailable")

    def trade_runner(*_args: object, **_kwargs: object) -> LiveTradeResult:
        raise failure

    controller = GuiController(trade_runner=trade_runner, os_name="nt")
    job_id = controller.start_trade(_trade_request(tmp_path, auto_confirm=True))

    assert controller.wait(1)
    assert controller.state is ControllerState.FAILED
    events = controller.drain_events()
    assert [(event.job_id, event.kind, event.payload) for event in events] == [
        (job_id, "failed", failure)
    ]
    assert not controller.acknowledge(job_id + 1)
    assert controller.acknowledge(job_id)


def test_double_start_is_rejected_until_cancelled_job_finishes(
    tmp_path: Path,
) -> None:
    started = threading.Event()

    def trade_runner(*_args: object, **kwargs: object) -> LiveTradeResult:
        cancel_event = kwargs["cancel_event"]
        started.set()
        assert cancel_event.wait(1)  # type: ignore[union-attr]
        raise OperationCancelled("cancelled in test")

    controller = GuiController(trade_runner=trade_runner, os_name="nt")
    request = _trade_request(tmp_path, auto_confirm=True)
    controller.start_trade(request)
    assert started.wait(1)

    with pytest.raises(RuntimeError, match="already running"):
        controller.start_trade(request)

    assert controller.cancel()
    assert controller.wait(1)
    assert controller.state is ControllerState.CANCELLED
    assert not controller.cancel()
    assert [event.kind for event in controller.drain_events()] == ["cancelled"]


def test_confirmation_can_be_approved_once_and_trade_succeeds(
    tmp_path: Path,
) -> None:
    callback_entered = threading.Event()
    decisions: list[bool] = []
    expected = LiveTradeResult(b"received", 0.5, tmp_path / "received.epa9")

    def trade_runner(*_args: object, **kwargs: object) -> LiveTradeResult:
        callback = kwargs["confirmation_callback"]
        callback_entered.set()
        decisions.append(callback(b"encrypted remote offer"))  # type: ignore[operator]
        return expected

    controller = GuiController(trade_runner=trade_runner, os_name="nt")
    job_id = controller.start_trade(_trade_request(tmp_path))
    assert callback_entered.wait(1)
    _wait_for(lambda: controller.state is ControllerState.AWAITING_CONFIRMATION)

    events = controller.drain_events()
    assert len(events) == 1
    assert events[0].job_id == job_id
    assert events[0].kind == "confirmation"
    prompt = events[0].payload
    assert prompt.offer == b"encrypted remote offer"
    assert controller.resolve_confirmation(prompt, True)
    assert not controller.resolve_confirmation(prompt, False)

    assert controller.wait(1)
    assert decisions == [True]
    assert controller.state is ControllerState.SUCCEEDED
    assert [event.kind for event in controller.drain_events()] == ["succeeded"]


def test_cancel_unblocks_pending_confirmation_and_emits_cancelled(
    tmp_path: Path,
) -> None:
    callback_entered = threading.Event()
    callback_cancelled = threading.Event()
    allow_worker_finish = threading.Event()

    def trade_runner(*_args: object, **kwargs: object) -> LiveTradeResult:
        callback = kwargs["confirmation_callback"]
        callback_entered.set()
        try:
            callback(b"encrypted remote offer")  # type: ignore[operator]
        except OperationCancelled:
            callback_cancelled.set()
            assert allow_worker_finish.wait(1)
            raise
        raise AssertionError("cancelled confirmation unexpectedly returned")

    controller = GuiController(trade_runner=trade_runner, os_name="nt")
    controller.start_trade(_trade_request(tmp_path))
    assert callback_entered.wait(1)
    _wait_for(lambda: controller.state is ControllerState.AWAITING_CONFIRMATION)

    assert controller.cancel()
    assert callback_cancelled.wait(1)
    assert controller.state is ControllerState.STOPPING
    prompt = next(
        event.payload
        for event in controller.drain_events()
        if event.kind == "confirmation"
    )
    assert not controller.resolve_confirmation(prompt, True)

    allow_worker_finish.set()
    assert controller.wait(1)
    assert controller.state is ControllerState.CANCELLED
    events = controller.drain_events()
    assert [event.kind for event in events] == ["cancelled"]
    assert isinstance(events[-1].payload, OperationCancelled)


def test_stale_confirmation_cannot_resolve_replacement_offer(tmp_path: Path) -> None:
    callback_ready = threading.Event()
    first_cancelled = threading.Event()
    allow_runner_finish = threading.Event()
    captured = {}
    expected = LiveTradeResult(b"received", 0.5, tmp_path / "received.epa9")

    def trade_runner(*_args: object, **kwargs: object) -> LiveTradeResult:
        callback = kwargs["confirmation_callback"]
        captured["callback"] = callback
        callback_ready.set()
        try:
            callback(b"offer-a")  # type: ignore[operator]
        except OperationCancelled:
            first_cancelled.set()
            assert allow_runner_finish.wait(1)
            return expected
        raise AssertionError("the first prompt should be replaced")

    controller = GuiController(trade_runner=trade_runner, os_name="nt")
    job_id = controller.start_trade(_trade_request(tmp_path))
    assert callback_ready.wait(1)
    _wait_for(lambda: controller.state is ControllerState.AWAITING_CONFIRMATION)
    first = next(
        event.payload
        for event in controller.drain_events()
        if event.kind == "confirmation"
    )

    second_result = {}

    def request_second() -> None:
        second_result["decision"] = captured["callback"](b"offer-b")

    second_thread = threading.Thread(target=request_second)
    second_thread.start()
    assert first_cancelled.wait(1)

    second = next(
        event.payload
        for event in controller.drain_events()
        if event.kind == "confirmation"
    )
    assert not controller.resolve_confirmation(first, True)
    assert controller.resolve_confirmation(second, False)
    second_thread.join(1)
    assert second_result == {"decision": False}

    allow_runner_finish.set()
    assert controller.wait(1)
    assert controller.state is ControllerState.SUCCEEDED
    assert controller.job_id == job_id
