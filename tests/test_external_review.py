"""ZA Trade Lab 0.8.0 review: synthetic, offline regression tests.

Run from the project root with its REAL dependencies installed:
    python -m pytest -q /path/to/test_review_regressions.py
On Linux, use xvfb-run -a for the one actual-Tk close-handler test.
No test opens a radio, connects to a console, or reads production keys.
Four tests expose the reported defects; four control tests should pass.
"""
from __future__ import annotations

from pathlib import Path
import threading
from unittest.mock import Mock

import pytest

import zasim.capture as capture
import zasim.client as client
import zasim.gui as gui
from zasim.control import OperationCancelled
from zasim.gui_controller import ControllerState
from zasim.pa9 import PA9
from zasim.reliable import ConflictingDuplicateError, ReliableFrame, ReliableReassembler


def _synthetic_party(species: int) -> bytes:
    record = PA9()
    record.species_internal = species
    record.stat_level = 50
    record.stat_hp_current = 100
    record.stat_hp_max = 100
    record.update_checksum()
    return record.to_bytes(encrypted=True)


def test_r1_retransmission_may_update_lowest_pending_ack() -> None:
    receiver = ReliableReassembler(next_sequence=100)
    assert receiver.feed(ReliableFrame(7, 101, 99, b"same-payload")) == ()
    # Same logical fragment, but the sender has learned a newer ACK watermark.
    assert receiver.feed(ReliableFrame(7, 101, 100, b"same-payload")) == ()
    delivered = receiver.feed(ReliableFrame(7, 100, 100, b"missing-payload"))
    assert [message.payload for message in delivered] == [
        b"missing-payload", b"same-payload"
    ]


def test_control_identical_buffered_duplicate_is_accepted() -> None:
    receiver = ReliableReassembler(next_sequence=100)
    frame = ReliableFrame(7, 101, 99, b"same-payload")
    assert receiver.feed(frame) == ()
    assert receiver.feed(frame) == ()


def test_control_changed_buffered_payload_is_still_rejected() -> None:
    receiver = ReliableReassembler(next_sequence=100)
    receiver.feed(ReliableFrame(7, 101, 99, b"original"))
    with pytest.raises(ConflictingDuplicateError):
        receiver.feed(ReliableFrame(7, 101, 99, b"different"))


def test_r2_late_competing_output_is_not_overwritten_and_trade_is_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "received.epa9"
    approved = client._preflight_trade_output(target, overwrite=False)
    competing_payload = _synthetic_party(133)
    incoming_payload = _synthetic_party(25)
    real_identity = client._target_identity
    injected = False

    def insert_competing_file_after_last_check(path: Path):
        nonlocal injected
        snapshot = real_identity(path)
        if path == target and snapshot is None and not injected:
            injected = True
            # A legal scheduling point: another process publishes after lstat
            # has returned but before this writer performs its final rename.
            with target.open("xb") as stream:
                stream.write(competing_payload)
        return snapshot

    monkeypatch.setattr(client, "_target_identity", insert_competing_file_after_last_check)
    saved, warning = client._persist_committed_party(incoming_payload, approved)
    assert injected, "the intended race boundary must have been exercised"
    assert target.read_bytes() == competing_payload, "unapproved competitor was overwritten"
    assert saved is not None and saved != target, "incoming data must get its own recovery path"
    assert PA9(saved.read_bytes(), encrypted=True).species_internal == 25
    assert warning, "the caller must learn that the requested output was not used"


def test_control_non_racing_trade_save_succeeds(tmp_path: Path) -> None:
    target = tmp_path / "received.epa9"
    approved = client._preflight_trade_output(target, overwrite=False)
    saved, warning = client._persist_committed_party(_synthetic_party(25), approved)
    assert saved == target
    assert warning is None
    assert PA9(target.read_bytes(), encrypted=True).species_internal == 25


def test_r3_close_must_not_discard_queued_indeterminate_trade_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tk = pytest.importorskip("tkinter")
    from tkinter import ttk, scrolledtext
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display is required (Linux: xvfb-run -a): {exc}")
    root.withdraw()
    messages = Mock()
    messages.askyesno.return_value = True
    original_destroy = root.destroy
    try:
        app = gui._DesktopApp(
            root, tk=tk, ttk=ttk, filedialog=Mock(), messagebox=messages,
            scrolledtext=scrolledtext, backend="relay", relay="127.0.0.1:47850",
            keys="", phyname="phy0", ifname="ldnclient", nickname="Review",
        )
        app.active_job_id = app.controller.job_id
        app.active_operation = "trade"
        result = client.LiveTradeResult(
            _synthetic_party(25), 1.0, tmp_path / "received.recovered-test.epa9",
            completed=False, warning="Outcome indeterminate; inspect before retrying",
        )
        app.controller._finish(
            app.controller.job_id, ControllerState.SUCCEEDED, "succeeded", result
        )
        # Schedule WM_DELETE_WINDOW between worker completion and the next poll.
        destroy = Mock(wraps=original_destroy)
        monkeypatch.setattr(root, "destroy", destroy)
        monkeypatch.setattr(app, "_editor_confirm_leave", lambda: True)
        app._request_close()
        assert not destroy.called or messages.showwarning.called, (
            "GUI was destroyed before presenting the queued indeterminate-result warning"
        )
    finally:
        try:
            original_destroy()
        except tk.TclError:
            pass


def test_r4_capture_cancellation_is_observed_while_skipping_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "metadata-only.jsonl"
    path.write_text('{"rec":"meta","event":"note"}\n' * 10_000)
    cancellation = threading.Event()
    real_iter_records = capture.iter_records
    consumed = 0

    def counted_records(*args, **kwargs):
        nonlocal consumed
        for record in real_iter_records(*args, **kwargs):
            consumed += 1
            if consumed == 3:
                cancellation.set()
            yield record

    monkeypatch.setattr(capture, "iter_records", counted_records)
    with pytest.raises(OperationCancelled):
        gui._collect_capture_analysis(path, preview=48, cancel_event=cancellation, limit=5)
    assert consumed <= 4, f"read {consumed} records despite cancellation at record 3"


def test_control_capture_analysis_yielded_packets_observe_cancellation(tmp_path: Path) -> None:
    path = tmp_path / "packets.jsonl"
    path.write_text('{"rec":"pkt"}\n' * 20)
    cancellation = threading.Event()
    cancellation.set()
    with pytest.raises(OperationCancelled):
        gui._collect_capture_analysis(path, preview=48, cancel_event=cancellation)
