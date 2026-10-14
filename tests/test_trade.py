from __future__ import annotations

import pytest

from zasim.trade import (
    IncomingResult,
    InvalidTransition,
    TradeNegotiation,
    TradeState,
    cancel_status_for_reason,
)
from zasim.trade_protocol import (
    CancelTrade,
    CompatibilityMatrix,
    ConfirmTrade,
    FinalAgreement,
    Ready,
    SelectPokemon,
    encode_frame,
)


PA9_A = bytes((index * 17 + 3) & 0xFF for index in range(0x158))
PA9_B = bytes((index * 29 + 5) & 0xFF for index in range(0x158))


def started(*, epoch: int = 0, delay: float = 1.5) -> TradeNegotiation:
    trade = TradeNegotiation(
        CompatibilityMatrix.empty(),
        initial_epoch=epoch,
        agreement_delay=delay,
    )
    emitted = trade.advance(0)
    assert len(emitted) == 1
    assert isinstance(emitted[0].command, Ready)
    trade.drain_actions()
    assert (
        trade.receive(Ready(epoch, CompatibilityMatrix.full()))
        is IncomingResult.ACCEPTED
    )
    assert trade.local_state is TradeState.SELECTING
    assert trade.remote_state is TradeState.SELECTING
    return trade


def test_initial_ready_is_emitted_once_and_waits_for_remote() -> None:
    matrix = CompatibilityMatrix.empty().set(3, 7)
    trade = TradeNegotiation(matrix, initial_epoch=12)

    first = trade.advance(4.0)
    assert len(first) == 1
    assert first[0].command == Ready(12, matrix)
    assert first[0].descriptor == b"\x01\x00"
    assert trade.local_state is TradeState.INITIAL_SYNC
    assert trade.remote_state is TradeState.INITIAL_SYNC
    assert trade.advance(4.5) == ()

    remote_matrix = CompatibilityMatrix.full()
    assert trade.receive(Ready(12, remote_matrix)) is IncomingResult.ACCEPTED
    assert trade.local_state is TradeState.SELECTING
    assert trade.remote_state is TradeState.SELECTING
    assert trade.remote_compatibility == remote_matrix
    assert trade.receive(Ready(12, remote_matrix)) is IncomingResult.ACCEPTED


def test_ready_can_arrive_before_local_update() -> None:
    trade = TradeNegotiation(CompatibilityMatrix.empty())
    assert (
        trade.receive(Ready(0, CompatibilityMatrix.full()))
        is IncomingResult.ACCEPTED
    )
    assert trade.local_state is TradeState.INITIAL_SYNC
    trade.advance(0)
    assert trade.local_state is TradeState.SELECTING
    assert trade.remote_state is TradeState.SELECTING


def test_select_normal_advances_but_transient_does_not() -> None:
    trade = started()

    transient = trade.select(PA9_A, transient=True)
    assert transient.command == SelectPokemon(0, PA9_A, True)
    assert trade.local_state is TradeState.SELECTING
    normal = trade.select(PA9_B)
    assert normal.command == SelectPokemon(0, PA9_B, False)
    assert trade.local_state is TradeState.OFFERED

    assert (
        trade.receive(SelectPokemon(0, PA9_A, True))
        is IncomingResult.ACCEPTED
    )
    assert trade.remote_state is TradeState.SELECTING
    assert trade.remote_offer == PA9_A
    assert trade.remote_committed_offer is None
    assert (
        trade.receive(SelectPokemon(0, PA9_B, False))
        is IncomingResult.ACCEPTED
    )
    assert trade.remote_state is TradeState.OFFERED
    assert trade.remote_offer == PA9_B
    assert trade.remote_committed_offer == PA9_B

    with pytest.raises(InvalidTransition):
        trade.select(PA9_A, transient=True)


def test_transient_select_updates_native_buffer_not_committed_offer() -> None:
    trade = started()

    assert (
        trade.receive(SelectPokemon(0, PA9_B, False))
        is IncomingResult.ACCEPTED
    )
    assert (
        trade.receive(SelectPokemon(0, PA9_A, True))
        is IncomingResult.ACCEPTED
    )
    assert trade.remote_state is TradeState.OFFERED
    assert trade.remote_offer == PA9_A
    assert trade.remote_committed_offer == PA9_B


def test_ready_and_select_callbacks_do_not_gate_on_epoch() -> None:
    trade = started(epoch=100)

    assert (
        trade.receive(SelectPokemon(99, PA9_A, True))
        is IncomingResult.ACCEPTED
    )
    assert (
        trade.receive(SelectPokemon(101, PA9_B))
        is IncomingResult.ACCEPTED
    )
    assert trade.remote_state is TradeState.OFFERED
    assert trade.remote_offer == PA9_B
    assert trade.remote_epoch == 100


def test_confirm_and_final_use_unsigned_minimum_epoch() -> None:
    trade = started(epoch=100)
    trade.select(PA9_A)
    trade.receive(SelectPokemon(7, PA9_B))

    assert (
        trade.receive(ConfirmTrade(99))
        is IncomingResult.STALE_SEQUENCE
    )
    assert trade.remote_state is TradeState.OFFERED
    assert trade.receive(ConfirmTrade(101)) is IncomingResult.ACCEPTED
    assert trade.remote_state is TradeState.CONFIRMED

    trade.confirm()
    assert trade.receive(FinalAgreement(99)) is IncomingResult.STALE_SEQUENCE
    assert trade.receive(FinalAgreement(101)) is IncomingResult.ACCEPTED
    assert trade.remote_state is TradeState.FINAL_AGREEMENT


def test_confirm_waits_full_delay_then_queues_final() -> None:
    trade = started()
    trade.select(PA9_A)
    trade.receive(SelectPokemon(0, PA9_B))
    trade.drain_actions()

    confirm = trade.confirm(now=10.0)
    assert confirm.command == ConfirmTrade(0)
    assert trade.local_state is TradeState.CONFIRMED
    assert trade.agreement_started_at is None
    assert trade.receive(ConfirmTrade(0), now=10.25) is IncomingResult.ACCEPTED
    assert trade.remote_state is TradeState.CONFIRMED
    assert trade.agreement_started_at == 10.25
    assert trade.advance(11.749) == ()

    emitted = trade.advance(11.75)
    assert len(emitted) == 1
    assert emitted[0].command == FinalAgreement(0)
    assert emitted[0].descriptor == b"\x01\x04"
    assert trade.local_state is TradeState.FINAL_AGREEMENT
    assert trade.advance(20) == ()

    assert trade.receive(FinalAgreement(0), now=20) is IncomingResult.ACCEPTED
    assert trade.remote_state is TradeState.FINAL_AGREEMENT
    assert trade.receive(FinalAgreement(0)) is IncomingResult.ACCEPTED


def test_repeated_confirm_command_restarts_native_agreement_timer() -> None:
    trade = started()
    trade.select(PA9_A)
    trade.receive(SelectPokemon(0, PA9_B))
    trade.confirm(now=10)
    trade.receive(ConfirmTrade(0), now=10.25)
    assert trade.agreement_started_at == 10.25

    trade.receive(ConfirmTrade(0), now=11)
    assert trade.agreement_started_at == 11
    assert trade.advance(12.49) == ()
    assert len(trade.advance(12.5)) == 1


@pytest.mark.parametrize(("reason", "status"), [(0, 0), (1, 2), (2, 1), (9, 0)])
def test_remote_cancel_advances_epoch_resets_and_maps_status(
    reason: int, status: int
) -> None:
    trade = started(epoch=41)
    trade.select(PA9_A)
    trade.receive(SelectPokemon(41, PA9_B))
    assert trade.pending_actions

    result = trade.receive(CancelTrade(42, reason))
    assert result is IncomingResult.ACCEPTED
    assert trade.local_epoch == 42
    assert trade.remote_epoch == 42
    expected_local = TradeState.OFFERED if status == 0 else TradeState.SELECTING
    assert trade.local_state is expected_local
    assert trade.remote_state is TradeState.SELECTING
    assert trade.local_offer == PA9_A
    assert trade.remote_offer is None
    assert trade.remote_committed_offer is None
    assert trade.cancel_status == status
    assert trade.remote_cancel_count == 1
    assert trade.last_remote_cancel_reason == reason
    assert trade.pending_actions == ()
    # The title callback has no sequence/duplicate gate; Reliable delivery is
    # responsible for invoking it only once.
    assert trade.receive(CancelTrade(42, reason)) is IncomingResult.ACCEPTED
    assert trade.local_epoch == 43
    assert trade.remote_cancel_count == 2


def test_remote_cancel_stores_arbitrary_epoch_without_title_level_gate() -> None:
    trade = started(epoch=8)
    trade.select(PA9_A)

    assert trade.receive(CancelTrade(8, 1)) is IncomingResult.ACCEPTED
    assert trade.local_state is TradeState.SELECTING
    assert trade.local_epoch == 9
    assert trade.remote_epoch == 8
    assert trade.pending_actions == ()

    assert trade.receive(CancelTrade(10, 0)) is IncomingResult.ACCEPTED
    assert trade.local_epoch == 10
    assert trade.remote_epoch == 10


def test_local_cancel_uses_successor_and_advances_both_epochs() -> None:
    trade = started(epoch=0xFFFF)
    trade.select(PA9_A)
    trade.receive(SelectPokemon(0xFFFF, PA9_B))

    action = trade.cancel(2)
    assert action.command == CancelTrade(0, 2)
    assert trade.local_epoch == 0
    assert trade.remote_epoch == 0
    assert trade.local_state is TradeState.SELECTING
    assert trade.remote_state is TradeState.SELECTING
    assert trade.local_offer == PA9_A
    assert trade.remote_offer is None
    assert trade.remote_committed_offer is None
    assert trade.cancel_status == 2
    assert trade.pending_actions == (action,)


def test_local_neutral_cancel_preserves_remote_offer_and_locked_state() -> None:
    trade = started(epoch=9)
    trade.select(PA9_A)
    trade.receive(SelectPokemon(9, PA9_B))

    action = trade.cancel(0)
    assert action.command == CancelTrade(10, 0)
    assert trade.local_state is TradeState.SELECTING
    assert trade.remote_state is TradeState.OFFERED
    assert trade.local_offer == PA9_A
    assert trade.remote_offer == PA9_B
    assert trade.remote_committed_offer == PA9_B


def test_local_cancel_is_rejected_in_native_states_two_and_five() -> None:
    trade = started()
    with pytest.raises(InvalidTransition):
        trade.cancel()

    trade.select(PA9_A)
    trade.receive(SelectPokemon(0, PA9_B))
    trade.confirm(now=0)
    trade.receive(ConfirmTrade(0), now=0)
    trade.advance(2)
    assert trade.local_state is TradeState.FINAL_AGREEMENT
    with pytest.raises(InvalidTransition):
        trade.cancel()


def test_receive_accepts_encoded_frame() -> None:
    trade = started()
    frame = encode_frame(SelectPokemon(0, PA9_B))
    assert trade.receive(frame) is IncomingResult.ACCEPTED
    assert trade.remote_offer == PA9_B
    assert trade.remote_committed_offer == PA9_B


def test_confirm_callback_has_no_remote_state_prerequisite() -> None:
    trade = started()

    assert trade.receive(ConfirmTrade(0)) is IncomingResult.ACCEPTED
    assert trade.remote_state is TradeState.CONFIRMED
    assert trade.receive(FinalAgreement(0)) is IncomingResult.INVALID_STATE
    assert trade.remote_state is TradeState.CONFIRMED
    assert trade.remote_offer is None
    with pytest.raises(InvalidTransition):
        trade.confirm()


def test_post_trade_reset_and_error_states() -> None:
    trade = started(delay=0)
    trade.select(PA9_A)
    trade.receive(SelectPokemon(0, PA9_B))
    trade.confirm()
    trade.receive(ConfirmTrade(0))
    trade.receive(FinalAgreement(0))
    assert trade.local_state is TradeState.FINAL_AGREEMENT
    assert trade.remote_state is TradeState.FINAL_AGREEMENT

    trade.finish_trade()
    assert trade.local_state is TradeState.POST_TRADE
    trade.reset_after_trade()
    assert trade.local_state is TradeState.SELECTING
    assert trade.remote_state is TradeState.SELECTING
    assert trade.remote_offer is None
    assert trade.remote_committed_offer is None
    trade.fail()
    assert trade.local_state is TradeState.ERROR


def test_clock_is_monotonic_and_cancel_mapping_is_exact() -> None:
    trade = TradeNegotiation(CompatibilityMatrix.empty())
    trade.advance(2)
    with pytest.raises(ValueError, match="backwards"):
        trade.advance(1)
    assert [cancel_status_for_reason(value) for value in (0, 1, 2, 3)] == [0, 2, 1, 0]
