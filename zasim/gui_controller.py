"""Headless controller shared by the Windows and Linux desktop GUI.

This module deliberately has no tkinter dependency.  Live work happens on one
worker thread, while the GUI drains immutable events and performs every widget
operation on its main thread.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import math
import os
from pathlib import Path
import sys
import threading
from typing import Callable, Literal

from zasim.ldn.ldn_name import validate_ldn_nickname

from .client import LiveTradeResult, TradeDeclined, run_live_trade
from .compatibility import DEFAULT_ZA_TITLE_VERSION, identification_token_for_title
from .control import CancellationToken, OperationCancelled, raise_if_cancelled
from .link_code import validate_link_code
from .live import run_passive_probe
from .relay import RelayTransport
from .radio_picker import RadioChoice, preflight_choice
from .user_data import make_pc_user_data


Backend = Literal["direct", "relay"]
FileIdentity = tuple[int, int, int, int]
EventKind = Literal[
    "log",
    "confirmation",
    "succeeded",
    "failed",
    "cancelled",
    "declined",
]


class ControllerState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    STOPPING = "stopping"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STATES = {
    ControllerState.RUNNING,
    ControllerState.AWAITING_CONFIRMATION,
    ControllerState.STOPPING,
}
TERMINAL_STATES = {
    ControllerState.SUCCEEDED,
    ControllerState.FAILED,
    ControllerState.CANCELLED,
}


def _finite_number(value: object, name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    minimum_ok = result >= 0 if allow_zero else result > 0
    if not math.isfinite(result) or not minimum_ok:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a finite {qualifier} number")
    return result


def _validate_expected_output_identity(
    value: FileIdentity | None, *, overwrite: bool
) -> None:
    if value is None:
        return
    if (
        not isinstance(value, tuple)
        or len(value) != 4
        or any(not isinstance(item, int) or isinstance(item, bool) for item in value)
    ):
        raise TypeError("expected_output_identity must be a four-integer tuple")
    if not overwrite:
        raise ValueError("expected_output_identity requires overwrite=True")


@dataclass(frozen=True, slots=True)
class ConnectionSettings:
    backend: Backend = "relay"
    relay_endpoint: str = "127.0.0.1:24872"
    keys_path: str = "~/.switch/prod.keys"
    phyname: str = "phy0"
    ifname: str = "ldnclient"
    nickname: str = "ZA-Trade"
    single_interface: bool | None = None
    selected_radio: RadioChoice | None = None
    bss_helper: str | None = None

    def validate(
        self,
        *,
        os_name: str | None = None,
        sys_platform: str | None = None,
    ) -> "ConnectionSettings":
        platform_name = os.name if os_name is None else os_name
        if sys_platform is None and os_name is not None:
            system_platform = "linux" if os_name == "posix" else os_name
        else:
            system_platform = sys.platform if sys_platform is None else sys_platform
        if self.backend not in ("direct", "relay"):
            raise ValueError("backend must be 'direct' or 'relay'")
        validate_ldn_nickname(self.nickname)
        if self.single_interface is not None and type(self.single_interface) is not bool:
            raise TypeError("single_interface must be bool or None")
        if self.bss_helper is not None:
            if not isinstance(self.bss_helper, str) or not self.bss_helper.strip():
                raise ValueError("BSS helper bundle directory cannot be empty")
            if self.backend != "direct" or self.single_interface is not True:
                raise ValueError("BSS helper requires local Linux and existing-interface reuse")
        if self.selected_radio is not None:
            if not isinstance(self.selected_radio, RadioChoice):
                raise TypeError("selected_radio must be a RadioChoice or None")
            if self.backend != "direct" or self.selected_radio.phy != self.phyname:
                raise ValueError("selected radio does not match the local connection")
            if self.single_interface is True and self.ifname != self.selected_radio.interface:
                raise ValueError("selected interface does not match the local connection")
            if self.single_interface is not True and self.ifname == self.selected_radio.interface:
                raise ValueError("a new virtual interface cannot replace the selected netdev")
        if self.backend == "direct":
            if platform_name == "nt" or not system_platform.startswith("linux"):
                raise ValueError(
                    "direct LDN is available only on Linux; select the relay backend"
                )
            if not self.keys_path.strip():
                raise ValueError("direct Linux mode requires a prod.keys path")
            if not self.phyname or any(
                character.isspace() for character in self.phyname
            ):
                raise ValueError("Wi-Fi PHY name cannot be empty or contain whitespace")
            if not self.ifname or any(
                character.isspace() for character in self.ifname
            ):
                raise ValueError("interface name cannot be empty or contain whitespace")
        else:
            # Construction performs endpoint, loopback, nickname, and title
            # profile validation without opening a socket.
            RelayTransport(
                self.relay_endpoint,
                nickname=self.nickname,
                log=lambda _message: None,
            )
        return self

    def live_kwargs(
        self,
        *,
        os_name: str | None = None,
        sys_platform: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, object]:
        self.validate(os_name=os_name, sys_platform=sys_platform)
        if self.bss_helper is not None:
            from zasim.ldn.bss_seed import validate_bundle
            validate_bundle(self.bss_helper)  # no module load; before keys/radio
        if self.selected_radio is not None:
            preflight_choice(self.selected_radio, cancel_event=cancel_event,
                             ifname=self.ifname, reuse=self.single_interface)
        result = {
            "keys_path": self.keys_path if self.backend == "direct" else None,
            "relay": self.relay_endpoint if self.backend == "relay" else None,
            "phyname": self.phyname,
            "ifname": self.ifname,
            "nickname": self.nickname,
        }
        if self.single_interface is not None:
            result["single_interface"] = self.single_interface
        if self.bss_helper is not None:
            result["bss_helper"] = self.bss_helper
        return result


@dataclass(frozen=True, slots=True)
class TradeRequest:
    connection: ConnectionSettings
    offer: Path
    output: Path
    link_code: str | None = None
    timeout: float = 300.0
    settle_grace: float = 5.0
    auto_confirm: bool = False
    overwrite: bool = False
    expected_output_identity: FileIdentity | None = None
    title_version: str = DEFAULT_ZA_TITLE_VERSION

    def validate(
        self, *, os_name: str | None = None, sys_platform: str | None = None
    ) -> "TradeRequest":
        identification_token_for_title(self.title_version)
        self.connection.validate(os_name=os_name, sys_platform=sys_platform)
        if not self.offer.is_file():
            raise ValueError(f"offer file does not exist: {self.offer}")
        if self.offer.resolve() == self.output.resolve():
            raise ValueError("offer and output must be different files")
        if not self.output.parent.is_dir():
            raise ValueError(f"output directory does not exist: {self.output.parent}")
        if self.output.is_symlink() or (
            self.output.exists() and not self.output.is_file()
        ):
            raise ValueError(f"trade output is not a regular file: {self.output}")
        if self.output.suffix.lower() not in {".pa9", ".epa9", ".ea9"}:
            raise ValueError("trade output must use .pa9, .epa9, or .ea9")
        validate_link_code(self.link_code)
        _finite_number(self.timeout, "trade timeout")
        _finite_number(self.settle_grace, "settle grace", allow_zero=True)
        if not isinstance(self.auto_confirm, bool):
            raise TypeError("auto_confirm must be a bool")
        if not isinstance(self.overwrite, bool):
            raise TypeError("overwrite must be a bool")
        _validate_expected_output_identity(
            self.expected_output_identity, overwrite=self.overwrite
        )
        return self


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    connection: ConnectionSettings
    output: Path
    link_code: str | None = None
    duration: float = 30.0
    overwrite: bool = False
    expected_output_identity: FileIdentity | None = None

    def validate(
        self, *, os_name: str | None = None, sys_platform: str | None = None
    ) -> "ProbeRequest":
        self.connection.validate(os_name=os_name, sys_platform=sys_platform)
        if not self.output.parent.is_dir():
            raise ValueError(f"capture directory does not exist: {self.output.parent}")
        if self.output.is_symlink() or (
            self.output.exists() and not self.output.is_file()
        ):
            raise ValueError(f"capture output is not a regular file: {self.output}")
        if self.output.suffix.lower() != ".jsonl":
            raise ValueError("capture output must use the .jsonl extension")
        validate_link_code(self.link_code)
        _finite_number(self.duration, "capture duration")
        if not isinstance(self.overwrite, bool):
            raise TypeError("overwrite must be a bool")
        _validate_expected_output_identity(
            self.expected_output_identity, overwrite=self.overwrite
        )
        return self


@dataclass(frozen=True, slots=True)
class GuiEvent:
    job_id: int
    kind: EventKind
    payload: object = None


@dataclass(slots=True)
class ConfirmationPrompt:
    job_id: int
    offer: bytes
    _done: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _decision: bool | None = field(default=None, repr=False)
    _cancelled: bool = field(default=False, repr=False)

    def resolve(self, decision: bool) -> bool:
        if not isinstance(decision, bool):
            raise TypeError("confirmation decision must be a bool")
        with self._lock:
            if self._done.is_set():
                return False
            self._decision = decision
            self._done.set()
            return True

    def cancel(self) -> bool:
        with self._lock:
            if self._done.is_set():
                return False
            self._cancelled = True
            self._done.set()
            return True

    def wait(self, cancel_event: CancellationToken) -> bool:
        while not self._done.wait(0.05):
            raise_if_cancelled(cancel_event)
        raise_if_cancelled(cancel_event)
        with self._lock:
            if self._cancelled or self._decision is None:
                raise OperationCancelled("trade confirmation was cancelled")
            return self._decision


TradeRunner = Callable[..., LiveTradeResult]
ProbeRunner = Callable[..., dict[str, int | str]]


class GuiController:
    """Thread-safe task controller; no method here touches tkinter."""

    MAX_BUFFERED_LOGS = 2000

    def __init__(
        self,
        *,
        trade_runner: TradeRunner = run_live_trade,
        probe_runner: ProbeRunner = run_passive_probe,
        os_name: str | None = None,
        sys_platform: str | None = None,
    ) -> None:
        self.trade_runner = trade_runner
        self.probe_runner = probe_runner
        self.os_name = os.name if os_name is None else os_name
        if sys_platform is None and os_name is not None:
            self.sys_platform = "linux" if os_name == "posix" else os_name
        else:
            self.sys_platform = sys.platform if sys_platform is None else sys_platform
        self._lock = threading.RLock()
        self._state = ControllerState.IDLE
        self._job_id = 0
        self._thread: threading.Thread | None = None
        self._cancel_event: threading.Event | None = None
        self._pending_confirmation: ConfirmationPrompt | None = None
        self._events: deque[GuiEvent] = deque()
        self._logs: deque[tuple[int, str]] = deque(maxlen=self.MAX_BUFFERED_LOGS)
        self._dropped_logs = 0

    @property
    def state(self) -> ControllerState:
        with self._lock:
            return self._state

    @property
    def job_id(self) -> int:
        with self._lock:
            return self._job_id

    @property
    def is_busy(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def has_pending_result(self) -> bool:
        """Worker completion does not acknowledge the result for the user."""
        return self.state in TERMINAL_STATES

    def _emit(self, job_id: int, kind: EventKind, payload: object = None) -> None:
        with self._lock:
            self._events.append(GuiEvent(job_id, kind, payload))

    def _log(self, job_id: int, message: object) -> None:
        text = str(message)
        with self._lock:
            if len(self._logs) == self.MAX_BUFFERED_LOGS:
                self._dropped_logs += 1
            self._logs.append((job_id, text))

    def drain_events(self) -> tuple[GuiEvent, ...]:
        with self._lock:
            events: list[GuiEvent] = []
            if self._dropped_logs:
                events.append(
                    GuiEvent(
                        self._job_id,
                        "log",
                        f"[gui] {self._dropped_logs} old log line(s) were discarded",
                    )
                )
                self._dropped_logs = 0
            events.extend(GuiEvent(job, "log", text) for job, text in self._logs)
            self._logs.clear()
            events.extend(self._events)
            self._events.clear()
            return tuple(events)

    def _begin(self, target: Callable[[int, threading.Event], None], name: str) -> int:
        with self._lock:
            if self._state in ACTIVE_STATES:
                raise RuntimeError("another GUI operation is already running")
            if self._state in TERMINAL_STATES:
                raise RuntimeError("acknowledge the previous GUI result before starting again")
            self._events.clear()
            self._logs.clear()
            self._dropped_logs = 0
            self._job_id += 1
            job_id = self._job_id
            cancel_event = threading.Event()
            self._cancel_event = cancel_event
            self._pending_confirmation = None
            self._state = ControllerState.RUNNING
            try:
                worker = threading.Thread(
                    target=target,
                    args=(job_id, cancel_event),
                    name=name,
                    daemon=False,
                )
                self._thread = worker
                worker.start()
            except BaseException:
                self._thread = None
                self._cancel_event = None
                self._state = ControllerState.IDLE
                raise
            return job_id

    def _finish(
        self,
        job_id: int,
        state: ControllerState,
        kind: EventKind,
        payload: object = None,
    ) -> None:
        with self._lock:
            if job_id != self._job_id:
                return
            prompt = self._pending_confirmation
            self._pending_confirmation = None
            if prompt is not None:
                prompt.cancel()
            self._state = state
            self._events.append(GuiEvent(job_id, kind, payload))

    def _confirmation_callback(
        self, job_id: int, cancel_event: threading.Event, offer: bytes
    ) -> bool:
        prompt = ConfirmationPrompt(job_id, bytes(offer))
        with self._lock:
            if job_id != self._job_id or cancel_event.is_set():
                raise OperationCancelled("trade confirmation was cancelled")
            previous = self._pending_confirmation
            if previous is not None:
                previous.cancel()
            self._pending_confirmation = prompt
            self._state = ControllerState.AWAITING_CONFIRMATION
            self._events.append(GuiEvent(job_id, "confirmation", prompt))
        try:
            return prompt.wait(cancel_event)
        finally:
            with self._lock:
                if self._pending_confirmation is prompt:
                    self._pending_confirmation = None
                    if (
                        job_id == self._job_id
                        and self._state == ControllerState.AWAITING_CONFIRMATION
                    ):
                        self._state = ControllerState.RUNNING

    def start_trade(self, request: TradeRequest) -> int:
        request.validate(os_name=self.os_name, sys_platform=self.sys_platform)

        def work(job_id: int, cancel_event: threading.Event) -> None:
            try:
                kwargs = request.connection.live_kwargs(
                    os_name=self.os_name, sys_platform=self.sys_platform, cancel_event=cancel_event
                )
                result = self.trade_runner(
                    request.offer,
                    peer_user_data=make_pc_user_data(request.connection.nickname),
                    sync_save=True,
                    title_version=request.title_version,
                    output=request.output,
                    overwrite=request.overwrite,
                    expected_output_identity=request.expected_output_identity,
                    auto_confirm=request.auto_confirm,
                    confirmation_callback=(
                        None
                        if request.auto_confirm
                        else lambda offer: self._confirmation_callback(
                            job_id, cancel_event, offer
                        )
                    ),
                    link_code=request.link_code,
                    timeout=request.timeout,
                    settle_grace=request.settle_grace,
                    cancel_event=cancel_event,
                    log=lambda message: self._log(job_id, message),
                    **kwargs,
                )
            except OperationCancelled as exc:
                self._finish(job_id, ControllerState.CANCELLED, "cancelled", exc)
            except TradeDeclined as exc:
                self._finish(job_id, ControllerState.CANCELLED, "declined", exc)
            except BaseException as exc:
                self._finish(job_id, ControllerState.FAILED, "failed", exc)
            else:
                self._finish(job_id, ControllerState.SUCCEEDED, "succeeded", result)

        return self._begin(work, "za-gui-trade")

    def start_probe(self, request: ProbeRequest) -> int:
        request.validate(os_name=self.os_name, sys_platform=self.sys_platform)

        def work(job_id: int, cancel_event: threading.Event) -> None:
            try:
                kwargs = request.connection.live_kwargs(
                    os_name=self.os_name, sys_platform=self.sys_platform, cancel_event=cancel_event
                )
                result = self.probe_runner(
                    output=request.output,
                    overwrite=request.overwrite,
                    expected_output_identity=request.expected_output_identity,
                    duration=request.duration,
                    link_code=request.link_code,
                    cancel_event=cancel_event,
                    log=lambda message: self._log(job_id, message),
                    **kwargs,
                )
            except OperationCancelled as exc:
                self._finish(job_id, ControllerState.CANCELLED, "cancelled", exc)
            except BaseException as exc:
                self._finish(job_id, ControllerState.FAILED, "failed", exc)
            else:
                self._finish(job_id, ControllerState.SUCCEEDED, "succeeded", result)

        return self._begin(work, "za-gui-probe")

    def is_confirmation_pending(self, prompt: ConfirmationPrompt) -> bool:
        with self._lock:
            return (
                prompt.job_id == self._job_id
                and self._pending_confirmation is prompt
                and self._state == ControllerState.AWAITING_CONFIRMATION
            )

    def resolve_confirmation(
        self, prompt: ConfirmationPrompt, decision: bool
    ) -> bool:
        with self._lock:
            if not self.is_confirmation_pending(prompt):
                return False
        return prompt.resolve(decision)

    def cancel(self) -> bool:
        with self._lock:
            if self._state not in ACTIVE_STATES or self._cancel_event is None:
                return False
            self._state = ControllerState.STOPPING
            self._cancel_event.set()
            if self._pending_confirmation is not None:
                self._pending_confirmation.cancel()
            return True

    def acknowledge(self, job_id: int) -> bool:
        with self._lock:
            if job_id != self._job_id or self._state not in TERMINAL_STATES:
                return False
            self._state = ControllerState.IDLE
            self._thread = None
            self._cancel_event = None
            self._pending_confirmation = None
            return True

    def wait(self, timeout: float | None = None) -> bool:
        with self._lock:
            worker = self._thread
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()


__all__ = [
    "ACTIVE_STATES",
    "ConnectionSettings",
    "ConfirmationPrompt",
    "ControllerState",
    "GuiController",
    "GuiEvent",
    "ProbeRequest",
    "TERMINAL_STATES",
    "TradeRequest",
]
