"""Independent synthetic regressions for the 0.8.0 external-review fixes.

No real transport, keys, personal save, or user desktop is accessed.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import multiprocessing
import os
from pathlib import Path
import threading
from unittest.mock import Mock

import pytest

from zasim import capture, client, file_save, gui, pa9_editor
from zasim.control import OperationCancelled
from zasim.gui_controller import ControllerState, GuiController
from zasim.pa9 import PA9
from zasim.reliable import (
    ConflictingDuplicateError, ReliableFlags, ReliableFrame, ReliableReassembler,
)


def _party(species: int) -> bytes:
    record = PA9()
    record.species_internal = species
    record.stat_level = 50
    record.stat_hp_current = record.stat_hp_max = 100
    record.update_checksum()
    return record.to_bytes(encrypted=True)


@pytest.mark.parametrize("sequence", [0, 100, 0xFFFF])
@pytest.mark.parametrize("watermarks", [(99, 100, 99), (100, 99, 100), (0xFFFF, 0, 0xFFFF)])
def test_retransmitted_out_of_order_fragments_deliver_exactly_once(sequence, watermarks):
    rx = ReliableReassembler(next_sequence=sequence)
    following = (sequence + 1) & 0xFFFF
    end = ReliableFrame(5, following, watermarks[0], b"-end", 3, (0b101,))
    for watermark in watermarks:
        assert rx.feed(replace(end, lowest_pending_ack=watermark)) == ()
    assert rx.pending_sequences == (following,)
    first = ReliableFrame(3, sequence, watermarks[-1], b"start", 3, (0b101,))
    messages = rx.feed(first)
    assert [item.payload for item in messages] == [b"start-end"]
    assert messages[0].fragment_count == 2
    assert rx.pending_sequences == ()
    for frame in [end, first, replace(end, lowest_pending_ack=123)]:
        assert rx.feed(frame) == ()


@pytest.mark.parametrize("change", [
    {"payload": b"changed"},
    {"flags": int(ReliableFlags.APPLICATION_DATA | ReliableFlags.MESSAGE_END)},
    {"flags": 7 | int(ReliableFlags.COMPRESSED)},
    {"destination_bit_count": 4},
    {"bitmap_words": (0b110,)},
])
def test_duplicate_contract_preserves_every_non_ack_field(change):
    rx = ReliableReassembler(next_sequence=10)
    original = ReliableFrame(7, 11, 8, b"same", 3, (0b101,))
    rx.feed(original)
    with pytest.raises(ConflictingDuplicateError):
        rx.feed(replace(original, lowest_pending_ack=9, **change))
    assert [m.payload for m in rx.feed(ReliableFrame(7, 10, 9, b"first"))] == [b"first", b"same"]


@pytest.mark.parametrize("suffix", [".pa9", ".epa9", ".ea9"])
def test_trade_publication_is_no_clobber_at_the_actual_os_boundary(tmp_path, monkeypatch, suffix):
    target = tmp_path / ("received" + suffix)
    approved = client._preflight_trade_output(target, overwrite=False)
    competitor = b"external-file-must-survive"
    operation = "rename" if os.name == "nt" else "link"
    real_publish = getattr(file_save.os, operation)
    reached = []

    def race(temporary, destination, *args, **kwargs):
        if Path(destination) == target:
            reached.append(True)
            with target.open("xb") as stream:
                stream.write(competitor)
        return real_publish(temporary, destination, *args, **kwargs)

    monkeypatch.setattr(file_save.os, operation, race)
    saved, warning = client._persist_committed_party(_party(25), approved)
    assert reached == [True]
    assert target.read_bytes() == competitor
    assert saved is not None and saved != target
    assert PA9(saved.read_bytes(), encrypted=suffix != ".pa9").species_internal == 25
    assert warning and str(saved) in warning
    assert not list(tmp_path.glob(".received.*.tmp"))


def test_trade_private_helper_does_not_implicitly_approve_overwrite(tmp_path):
    target = tmp_path / "received.epa9"
    target.write_bytes(b"not-approved")
    with pytest.raises(FileExistsError):
        client._atomic_save_received_party(_party(25), target)
    assert target.read_bytes() == b"not-approved"


def _parallel_trade_save(path, species, barrier, results, overwrite):
    try:
        approved = client._preflight_trade_output(path, overwrite=overwrite)
        barrier.wait(timeout=15)
        saved, warning = client._persist_committed_party(_party(species), approved)
        results.put((species, str(saved), warning, None))
    except BaseException as exc:
        results.put((species, None, None, repr(exc)))


@pytest.mark.parametrize("overwrite", [False, True])
def test_two_processes_preserve_both_trade_outputs(tmp_path, overwrite):
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    target = tmp_path / "received.epa9"
    if overwrite:
        target.write_bytes(_party(152))
    workers = [ctx.Process(target=_parallel_trade_save, args=(str(target), species, barrier, results, overwrite))
               for species in (25, 133)]
    try:
        for worker in workers:
            worker.start()
        received = [results.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0
        assert all(error is None for _, _, _, error in received), received
        paths = [Path(path) for _, path, _, _ in received]
        assert len(set(paths)) == 2
        assert target in paths
        for species, path, warning, _ in received:
            assert PA9(Path(path).read_bytes(), encrypted=True).species_internal == species
            assert (warning is not None) == (Path(path) != target)
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
        results.close()
        results.join_thread()


def _hold_editor_lock(path, ready, release):
    target = pa9_editor._preflight_output(path, overwrite=True)
    with pa9_editor._exclusive_save_lock(target):
        ready.set()
        assert release.wait(20)


def test_editor_process_lock_excludes_trade_writer_and_releases(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    target = tmp_path / "received.epa9"
    target.write_bytes(_party(133))
    approved = client._preflight_trade_output(target, overwrite=True)
    worker = ctx.Process(target=_hold_editor_lock, args=(str(target), ready, release))
    worker.start()
    try:
        assert ready.wait(15)
        saved, warning = client._persist_committed_party(_party(25), approved)
        assert saved is not None and saved != target and warning
        assert PA9(target.read_bytes(), encrypted=True).species_internal == 133
        assert PA9(saved.read_bytes(), encrypted=True).species_internal == 25
    finally:
        release.set()
        worker.join(10)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
    assert worker.exitcode == 0
    # Crash-released OS locks and same-process locks must both be released.
    saved, warning = client._persist_committed_party(_party(152), approved)
    assert saved == target and warning is None
    assert PA9(target.read_bytes(), encrypted=True).species_internal == 152


def test_trade_lock_excludes_editor_writer(tmp_path):
    target = tmp_path / "shared.epa9"
    doc = pa9_editor.PA9EditorDocument(PA9(_party(25), encrypted=True))
    with file_save.exclusive_save_lock(target):
        with pytest.raises(FileExistsError, match="already in progress"):
            doc.save(target)
    assert not target.exists()
    assert doc.save(target) == target


def test_saved_new_editor_document_can_be_saved_again(tmp_path):
    target = tmp_path / "new.epa9"
    doc = pa9_editor.PA9EditorDocument(PA9(_party(25), encrypted=True))
    doc.save(target)
    assert doc.source_identity == pa9_editor.file_identity(target)
    doc.apply({"nickname": "Second"})
    doc.save()
    assert PA9(target.read_bytes(), encrypted=True).nickname == "Second"


def test_trade_detects_external_change_after_publication_and_recovers(tmp_path, monkeypatch):
    target = tmp_path / "received.epa9"
    approved = client._preflight_trade_output(target, overwrite=False)
    real_publish = client.publish_approved

    def publish_then_change(*args, **kwargs):
        real_publish(*args, **kwargs)
        target.write_bytes(_party(133))

    monkeypatch.setattr(client, "publish_approved", publish_then_change)
    saved, warning = client._persist_committed_party(_party(25), approved)
    assert saved is not None and saved != target and warning
    assert PA9(target.read_bytes(), encrypted=True).species_internal == 133
    assert PA9(saved.read_bytes(), encrypted=True).species_internal == 25


def test_trade_overwrite_checks_content_even_when_size_and_mtime_match(tmp_path):
    target = tmp_path / "received.epa9"
    target.write_bytes(_party(133))
    approved = client._preflight_trade_output(target, overwrite=True)
    metadata = target.stat()
    target.write_bytes(_party(152))
    os.utime(target, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    saved, warning = client._persist_committed_party(_party(25), approved)
    assert saved is not None and saved != target and warning
    assert PA9(target.read_bytes(), encrypted=True).species_internal == 152


@pytest.fixture
def desktop():
    tk = pytest.importorskip("tkinter")
    from tkinter import ttk, scrolledtext
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"mapped Tk display required: {exc}")
    messages = Mock()
    messages.askyesno.return_value = True
    app = gui._DesktopApp(
        root, tk=tk, ttk=ttk, filedialog=Mock(), messagebox=messages,
        scrolledtext=scrolledtext, backend="relay", relay="127.0.0.1:47850",
        keys="", phyname="phy0", ifname="ldnclient", nickname="Synthetic",
    )
    root.update()
    yield app, root, messages
    try:
        root.destroy()
    except tk.TclError:
        pass


def _queue_uncertain_result(app, tmp_path):
    app.active_job_id = app.controller.job_id
    app.active_operation = "trade"
    result = client.LiveTradeResult(
        _party(25), 1.0, tmp_path / "received.recovered-test.epa9",
        completed=False, warning="Outcome indeterminate; inspect before retrying",
    )
    app.controller._finish(app.active_job_id, ControllerState.SUCCEEDED, "succeeded", result)
    return result


@pytest.mark.parametrize("language", ["ja", "en"])
def test_close_shows_uncertain_result_and_recovery_path_before_destroy(desktop, tmp_path, monkeypatch, language):
    app, root, messages = desktop
    app.change_language(language)
    result = _queue_uncertain_result(app, tmp_path)
    actions = []
    messages.showwarning.side_effect = lambda *_args, **_kw: actions.append("warning")
    real_destroy = root.destroy
    monkeypatch.setattr(root, "destroy", lambda: (actions.append("destroy"), real_destroy()))
    app._request_close()
    assert app.close_when_idle and actions == []
    assert app.controller.has_pending_result
    app._poll()
    assert actions == ["warning", "destroy"]
    assert str(result.output_path) in messages.showwarning.call_args.args[1]
    assert result.warning in messages.showwarning.call_args.args[1]
    assert not app.controller.has_pending_result


def test_dialog_failure_retains_result_until_successful_presentation(desktop, tmp_path, monkeypatch):
    app, root, messages = desktop
    _queue_uncertain_result(app, tmp_path)
    messages.showwarning.side_effect = [RuntimeError("synthetic dialog failure"), None]
    destroy = Mock(wraps=root.destroy)
    monkeypatch.setattr(root, "destroy", destroy)
    app._request_close()
    app._poll()
    assert not destroy.called
    assert app.controller.has_pending_result and app.active_job_id is not None
    assert app._pending_terminal_event is not None
    app._request_close()  # A repeated exit must not silently acknowledge it.
    assert not destroy.called
    app._poll()
    assert messages.showwarning.call_count == 2
    assert destroy.call_count == 1
    assert app._pending_terminal_event is None
    assert not app.controller.has_pending_result


def test_close_waits_for_event_queued_just_after_drain(desktop, tmp_path, monkeypatch):
    app, root, messages = desktop
    app.active_job_id, app.active_operation = app.controller.job_id, "trade"
    app.close_when_idle = True
    real_drain = app.controller.drain_events
    destroy = Mock(wraps=root.destroy)
    monkeypatch.setattr(root, "destroy", destroy)

    def delayed_finish():
        _queue_uncertain_result(app, tmp_path)
        return ()

    monkeypatch.setattr(app.controller, "drain_events", delayed_finish)
    app._poll()
    assert not destroy.called and app.controller.has_pending_result
    monkeypatch.setattr(app.controller, "drain_events", real_drain)
    app._poll()
    assert messages.showwarning.call_count == 1 and destroy.call_count == 1


def test_new_controller_job_cannot_discard_unacknowledged_terminal():
    controller = GuiController()
    result = object()
    controller._finish(0, ControllerState.SUCCEEDED, "succeeded", result)
    with pytest.raises(RuntimeError, match="acknowledge"):
        controller._begin(lambda *_: pytest.fail("must not start"), "forbidden")
    assert controller.drain_events()[0].payload is result
    assert controller.has_pending_result
    assert controller.acknowledge(0)
    assert not controller.has_pending_result


@pytest.mark.parametrize("line", [b"\n", b" \t\r\n", b'{"rec":"meta","event":"note"}\n'])
def test_cancel_stops_input_reads_including_blank_lines(tmp_path, monkeypatch, line):
    target = tmp_path / "input.jsonl"
    target.write_bytes(line * 10000)
    real_open = capture.open_regular_binary
    cancellation = threading.Event()
    reads = []

    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def readline(self, limit):
            reads.append(limit)
            result = self.stream.readline(limit)
            if len(reads) == 3:
                cancellation.set()
            return result

    @contextmanager
    def open_counted(path, *args, **kwargs):
        with real_open(path, *args, **kwargs) as stream:
            yield Reader(stream) if Path(path) == target else stream

    monkeypatch.setattr(capture, "open_regular_binary", open_counted)
    with pytest.raises(OperationCancelled):
        gui._collect_capture_analysis(target, preview=48, cancel_event=cancellation)
    assert len(reads) == 3
    assert all(limit == capture.MAX_CAPTURE_LINE_BYTES + 1 for limit in reads)


def test_precancelled_capture_does_not_open_input(tmp_path):
    cancellation = threading.Event()
    cancellation.set()
    with pytest.raises(OperationCancelled):
        list(capture.iter_records(tmp_path / "nonexistent", cancel_event=cancellation))
    with pytest.raises(OperationCancelled):
        list(capture.analyze_capture(tmp_path / "nonexistent", cancel_event=cancellation))


def test_capture_optional_argument_preserves_original_api(tmp_path):
    path = tmp_path / "capture.jsonl"
    path.write_text('\n{"rec":"meta","event":"note"}\n{"rec":"pkt"}\n')
    assert len(list(capture.iter_records(path))) == 2
    assert list(capture.analyze_capture(path)) == list(capture.analyze_capture(path, cancel_event=None))
    with pytest.raises(TypeError, match="is_set"):
        list(capture.iter_records(path, cancel_event=object()))
