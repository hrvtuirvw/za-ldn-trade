"""Deterministic Pokemon Legends: Z-A trade negotiation state machine.

This module models the title-level ``CommandReady`` through
``CommandFinalAgreement`` exchange.  It intentionally stops at the boundary
where the encoded command frame is handed to Pia; it performs no discovery,
session management, reliable delivery, or network I/O.

The state and epoch rules are based on the executable's ``TradeClient``.  A
caller drives time explicitly with :meth:`TradeNegotiation.advance`, making the
1.5-second final-agreement delay deterministic in tests and command-line tools.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum, IntEnum
import math
from typing import TypeAlias

from .trade_protocol import (
    CancelTrade,
    CompatibilityMatrix,
    ConfirmTrade,
    FinalAgreement,
    Ready,
    SelectPokemon,
    TradeCommand,
    TradeFrame,
    decode_frame,
    encode_frame,
)


UINT16_MASK = 0xFFFF
DEFAULT_AGREEMENT_DELAY = 1.5


class TradeState(IntEnum):
    """Recovered numeric values stored in ``TradeClient`` state fields."""

    INITIAL_SYNC = 1
    SELECTING = 2
    OFFERED = 3
    CONFIRMED = 4
    FINAL_AGREEMENT = 5
    POST_TRADE = 6
    ERROR = 7


class IncomingResult(Enum):
    """Disposition of a syntactically valid incoming trade command."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    STALE_SEQUENCE = "stale-sequence"
    FUTURE_SEQUENCE = "future-sequence"
    INVALID_STATE = "invalid-state"


class InvalidTransition(RuntimeError):
    """A local UI action is not valid in the current negotiation state."""


@dataclass(frozen=True)
class OutboundAction:
    """One queued title command and its transport-neutral encoded frame."""

    command: TradeCommand
    frame: TradeFrame

    @classmethod
    def from_command(cls, command: TradeCommand) -> "OutboundAction":
        return cls(command, encode_frame(command))

    @property
    def descriptor(self) -> bytes:
        return self.frame.descriptor

    @property
    def payload(self) -> bytes:
        return self.frame.payload


IncomingCommand: TypeAlias = TradeCommand | TradeFrame


def _u16(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value <= UINT16_MASK:
        raise ValueError(f"{name} must be from 0 through 65535")
    return value


def _next_epoch(value: int) -> int:
    return (value + 1) & UINT16_MASK


def cancel_status_for_reason(reason: int) -> int:
    """Return the title's observed local status mapping for a remote cancel."""

    if not isinstance(reason, int) or isinstance(reason, bool):
        raise TypeError("reason must be an integer")
    if reason == 1:
        return 2
    if reason == 2:
        return 1
    return 0


class TradeNegotiation:
    """Pure state machine for one Z-A link-trade command endpoint.

    Constructing the object does not enqueue traffic.  Call ``advance(now)``
    once to emit the initial :class:`Ready`, then send actions returned by
    :meth:`drain_actions` through the caller's transport.  Incoming frames can
    be passed directly to :meth:`receive`.
    """

    def __init__(
        self,
        compatibility: CompatibilityMatrix,
        *,
        initial_epoch: int = 0,
        agreement_delay: float = DEFAULT_AGREEMENT_DELAY,
    ) -> None:
        if not isinstance(compatibility, CompatibilityMatrix):
            raise TypeError("compatibility must be a CompatibilityMatrix")
        initial_epoch = _u16(initial_epoch, "initial_epoch")
        if (
            not isinstance(agreement_delay, (int, float))
            or isinstance(agreement_delay, bool)
            or not math.isfinite(float(agreement_delay))
            or agreement_delay < 0
        ):
            raise ValueError("agreement_delay must be a finite non-negative number")

        self.compatibility = compatibility.copy()
        self.local_state = TradeState.INITIAL_SYNC
        self.remote_state = TradeState.INITIAL_SYNC
        self.local_epoch = initial_epoch
        self.remote_epoch = initial_epoch
        self.local_offer: bytes | None = None
        # ``remote_offer`` mirrors the title's receive buffer and is replaced
        # by every SelectPokemon callback, including transient/clear updates.
        # Consumers that may commit or persist a trade must instead use the
        # last non-transient selection.
        self.remote_offer: bytes | None = None
        self.remote_committed_offer: bytes | None = None
        self.remote_compatibility: CompatibilityMatrix | None = None
        self.cancel_status = 0
        self.remote_cancel_count = 0
        self.last_remote_cancel_reason: int | None = None

        self.agreement_delay = float(agreement_delay)
        self._now = 0.0
        self._agreement_started_at: float | None = None
        self._ready_sent = False
        self._remote_ready = False
        self._actions: deque[OutboundAction] = deque()

    @property
    def now(self) -> float:
        return self._now

    @property
    def ready_sent(self) -> bool:
        return self._ready_sent

    @property
    def remote_ready(self) -> bool:
        return self._remote_ready

    @property
    def agreement_started_at(self) -> float | None:
        return self._agreement_started_at

    @property
    def pending_actions(self) -> tuple[OutboundAction, ...]:
        """A non-destructive snapshot of the deterministic send queue."""

        return tuple(self._actions)

    def drain_actions(self) -> tuple[OutboundAction, ...]:
        """Remove and return all actions in send order."""

        actions = tuple(self._actions)
        self._actions.clear()
        return actions

    def pop_action(self) -> OutboundAction | None:
        """Remove the next action, or return ``None`` when the queue is empty."""

        if not self._actions:
            return None
        return self._actions.popleft()

    def _touch(self, now: float | None) -> None:
        if now is None:
            return
        if (
            not isinstance(now, (int, float))
            or isinstance(now, bool)
            or not math.isfinite(float(now))
        ):
            raise ValueError("now must be a finite number")
        value = float(now)
        if value < self._now:
            raise ValueError("now cannot move backwards")
        self._now = value

    def _emit(self, command: TradeCommand) -> OutboundAction:
        action = OutboundAction.from_command(command)
        self._actions.append(action)
        return action

    @staticmethod
    def _minimum_sequence_result(received: int, minimum: int) -> IncomingResult:
        """Mirror the title callbacks' plain unsigned ``minimum <= received``."""

        return (
            IncomingResult.ACCEPTED
            if received >= minimum
            else IncomingResult.STALE_SEQUENCE
        )

    def _enter_selecting_if_ready(self) -> None:
        if (
            self.local_state is TradeState.INITIAL_SYNC
            and self._ready_sent
            and self._remote_ready
        ):
            # Native 2.0.2 tick 0x95f6bc..0x95f6e0 discards an offer
            # committed before initial sync finishes. A preview keeps the
            # prior remote state below 3, so that receive buffer survives.
            if self.remote_state >= TradeState.OFFERED:
                self.remote_offer = None
                self.remote_committed_offer = None
            self.local_state = TradeState.SELECTING
            self.remote_state = TradeState.SELECTING

    def _refresh_agreement_timer(self) -> None:
        both_agreed = (
            self.local_state in (TradeState.CONFIRMED, TradeState.FINAL_AGREEMENT)
            and self.remote_state
            in (TradeState.CONFIRMED, TradeState.FINAL_AGREEMENT)
        )
        if both_agreed:
            if self._agreement_started_at is None:
                self._agreement_started_at = self._now
        else:
            self._agreement_started_at = None

    def _emit_final_if_due(self) -> OutboundAction | None:
        self._refresh_agreement_timer()
        if (
            self.local_state is TradeState.CONFIRMED
            and self._agreement_started_at is not None
            and self._now - self._agreement_started_at >= self.agreement_delay
        ):
            action = self._emit(FinalAgreement(self.local_epoch))
            self.local_state = TradeState.FINAL_AGREEMENT
            return action
        return None

    def advance(self, now: float) -> tuple[OutboundAction, ...]:
        """Advance logical time and enqueue any commands that become due."""

        self._touch(now)
        emitted: list[OutboundAction] = []
        if self.local_state is TradeState.INITIAL_SYNC and not self._ready_sent:
            emitted.append(
                self._emit(Ready(self.local_epoch, self.compatibility.copy()))
            )
            self._ready_sent = True
            self._enter_selecting_if_ready()
        final = self._emit_final_if_due()
        if final is not None:
            emitted.append(final)
        return tuple(emitted)

    def select(
        self,
        party_pa9: bytes | bytearray | memoryview,
        *,
        transient: bool = False,
    ) -> OutboundAction:
        """Queue a local Pokemon selection at the current epoch."""

        if not isinstance(transient, bool):
            raise TypeError("transient must be a bool")
        # The recovered sender gates both normal and transient selections on
        # native state 2.  A transient update after locking an offer would not
        # be emitted by the title.
        if self.local_state is not TradeState.SELECTING:
            raise InvalidTransition(
                f"cannot select a Pokemon while local state is {self.local_state.name}"
            )
        command = SelectPokemon(self.local_epoch, bytes(party_pa9), transient)
        action = self._emit(command)
        self.local_offer = command.party_pa9
        if not transient:
            self.local_state = TradeState.OFFERED
        return action

    def confirm(self, *, now: float | None = None) -> OutboundAction:
        """Confirm the local offer and start the agreement timer if paired."""

        self._touch(now)
        if self.local_state is not TradeState.OFFERED:
            raise InvalidTransition(
                f"cannot confirm while local state is {self.local_state.name}"
            )
        action = self._emit(ConfirmTrade(self.local_epoch))
        self.local_state = TradeState.CONFIRMED
        self._emit_final_if_due()
        return action

    def _reset_to_selecting(self, *, clear_actions: bool) -> None:
        if clear_actions:
            self._actions.clear()
        self.local_state = TradeState.SELECTING
        self.remote_state = TradeState.SELECTING
        self.local_offer = None
        self.remote_offer = None
        self.remote_committed_offer = None
        self._agreement_started_at = None

    def cancel(self, reason: int = 0) -> OutboundAction:
        """Send a local cancel barrier and mirror the title's UI-state reset.

        Reason 0 is the neutral cancel/reselection path.  Recovered reasons 1
        and 2 report a local- or remote-Pokemon legality problem respectively.
        """

        if self.local_state in (
            TradeState.SELECTING,
            TradeState.FINAL_AGREEMENT,
        ):
            raise InvalidTransition(
                f"cannot cancel while local state is {self.local_state.name}"
            )
        sequence = _next_epoch(self.local_epoch)
        command = CancelTrade(sequence, reason)
        self.local_epoch = sequence
        self.remote_epoch = _next_epoch(self.remote_epoch)
        self.cancel_status = command.reason
        self._agreement_started_at = None
        self._actions.clear()
        self.local_state = TradeState.SELECTING
        if self.remote_state in (
            TradeState.OFFERED,
            TradeState.CONFIRMED,
            TradeState.FINAL_AGREEMENT,
        ):
            if command.reason == 0:
                self.remote_state = TradeState.OFFERED
            else:
                self.remote_state = TradeState.SELECTING
                self.remote_offer = None
                self.remote_committed_offer = None
        return self._emit(command)

    def receive(
        self, incoming: IncomingCommand, *, now: float | None = None
    ) -> IncomingResult:
        """Apply one decoded command or frame without performing network I/O."""

        self._touch(now)
        command: TradeCommand
        if isinstance(incoming, TradeFrame):
            command = decode_frame(incoming)
        elif isinstance(
            incoming,
            (Ready, SelectPokemon, ConfirmTrade, CancelTrade, FinalAgreement),
        ):
            command = incoming
        else:
            raise TypeError("incoming must be a trade command or TradeFrame")

        if isinstance(command, Ready):
            # Ready and Select callbacks do not read command+0 (sequence).
            result = self._receive_ready(command)
        elif isinstance(command, SelectPokemon):
            result = self._receive_select(command)
        elif isinstance(command, CancelTrade):
            result = self._receive_cancel(command)
        else:
            # Confirm and FinalAgreement use a plain unsigned lower-bound
            # comparison in the executable, not equality or RFC1982 math.
            result = self._minimum_sequence_result(
                command.sequence, self.remote_epoch
            )
            if result is not IncomingResult.ACCEPTED:
                return result
            if isinstance(command, ConfirmTrade):
                result = self._receive_confirm()
            else:
                result = self._receive_final()

        if result is IncomingResult.ACCEPTED and isinstance(command, ConfirmTrade):
            self._emit_final_if_due()
        return result

    def _receive_ready(self, command: Ready) -> IncomingResult:
        self.remote_compatibility = command.matrix.copy()
        self._remote_ready = True
        self._enter_selecting_if_ready()
        return IncomingResult.ACCEPTED

    def _receive_select(self, command: SelectPokemon) -> IncomingResult:
        # The callback replaces the remote Pokemon for both transient and
        # normal commands without inspecting the prior remote state.
        self.remote_offer = command.party_pa9
        if command.preview:
            return IncomingResult.ACCEPTED
        self.remote_committed_offer = command.party_pa9
        self.remote_state = TradeState.OFFERED
        return IncomingResult.ACCEPTED

    def _receive_confirm(self) -> IncomingResult:
        # The native callback sets state 4 without checking the preceding
        # remote state.  Reliable ordering normally makes that OFFERED.
        self.remote_state = TradeState.CONFIRMED
        if self.local_state is TradeState.CONFIRMED:
            # Each accepted Confirm restarts the native 1.5-second timer.
            self._agreement_started_at = self._now
        return IncomingResult.ACCEPTED

    def _receive_final(self) -> IncomingResult:
        # Native gates this callback only on the local confirmed/final phase.
        if self.local_state not in (
            TradeState.CONFIRMED,
            TradeState.FINAL_AGREEMENT,
        ):
            return IncomingResult.INVALID_STATE
        self.remote_state = TradeState.FINAL_AGREEMENT
        return IncomingResult.ACCEPTED

    def _receive_cancel(self, command: CancelTrade) -> IncomingResult:
        # Reliable delivers each application command once.  The title-level
        # Cancel callback itself deliberately has no sequence gate: it stores
        # the received epoch and advances the local barrier.
        prior_remote_state = self.remote_state
        self.remote_cancel_count += 1
        self.last_remote_cancel_reason = command.reason
        self.remote_epoch = command.sequence
        self.local_epoch = _next_epoch(self.local_epoch)
        self.cancel_status = cancel_status_for_reason(command.reason)
        self._agreement_started_at = None
        self._actions.clear()
        if prior_remote_state > TradeState.SELECTING:
            self.remote_offer = None
            self.remote_committed_offer = None
        self.remote_state = TradeState.SELECTING

        # The local selection buffer is retained by the native callback.  A
        # neutral cancel keeps/returns the UI to locked state 3; status-bearing
        # reasons unlock it to state 2.  Retaining local_offer also prevents
        # the active adapter from immediately resending SelectPokemon.
        if self.local_state in (
            TradeState.OFFERED,
            TradeState.CONFIRMED,
            TradeState.FINAL_AGREEMENT,
        ):
            self.local_state = (
                TradeState.OFFERED
                if self.cancel_status == 0
                else TradeState.SELECTING
            )
        return IncomingResult.ACCEPTED

    def finish_trade(self) -> None:
        """Enter recovered state 6 after both final agreements are present."""

        if not (
            self.local_state is TradeState.FINAL_AGREEMENT
            and self.remote_state is TradeState.FINAL_AGREEMENT
        ):
            raise InvalidTransition("both peers must reach final agreement first")
        self.local_state = TradeState.POST_TRADE

    def reset_after_trade(self) -> None:
        """Model the observed state-6 reset back to unlocked state 2."""

        if self.local_state is not TradeState.POST_TRADE:
            raise InvalidTransition("local state is not POST_TRADE")
        self._reset_to_selecting(clear_actions=True)

    def fail(self) -> None:
        """Enter recovered terminal/error state 7 and discard queued traffic."""

        self.local_state = TradeState.ERROR
        self._agreement_started_at = None
        self._actions.clear()


__all__ = [
    "DEFAULT_AGREEMENT_DELAY",
    "IncomingCommand",
    "IncomingResult",
    "InvalidTransition",
    "OutboundAction",
    "TradeNegotiation",
    "TradeState",
    "cancel_status_for_reason",
]
