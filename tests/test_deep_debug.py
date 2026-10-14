"""Synthetic fault-injection regressions; no real radios, keys or saved data."""
from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import zatrade
from zasim import capture, file_save, i18n, pa9_editor
from zasim.gui_controller import ControllerState, GuiController
from zasim.pa9 import PA9
from test_review_hardening import desktop, _queue_uncertain_result


def test_corrupt_deep_preferences_do_not_prevent_startup(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    raw = '{"other":' + '[' * 2000 + '0' + ']' * 2000 + '}'
    path.write_text(raw)
    monkeypatch.delenv("ZA_TRADE_LANG", raising=False)
    monkeypatch.setattr(i18n, "preference_path", lambda: path)
    monkeypatch.setattr(i18n, "detect_language", lambda: "en")
    assert i18n.preferred_language() == "en"
    assert path.read_text() == raw
    with pytest.raises(ValueError):
        i18n.save_language("ja", path=path)
    assert path.read_text() == raw


def test_capture_deep_json_is_a_clean_bounded_input_error(tmp_path):
    path = tmp_path / "deep.jsonl"
    path.write_text('{"rec":"meta","nested":' + '[' * 2000 + '0' + ']' * 2000 + '}\n')
    with pytest.raises(ValueError):
        list(capture.iter_records(path))


def test_pa9_identity_does_not_hash_arbitrarily_large_files(tmp_path):
    target = tmp_path / "oversized.epa9"
    with target.open("wb") as stream:
        stream.truncate(1024 * 1024 + 1)
    with pytest.raises(ValueError):
        file_save.file_identity(target)


def test_cli_command_blob_read_has_a_resource_limit(tmp_path):
    target = tmp_path / "huge.bin"
    with target.open("wb") as stream:
        stream.truncate(65537)
    args = SimpleNamespace(input_hex=None, input_file=str(target))
    with pytest.raises(ValueError):
        zatrade._binary_arg(args, "input")


def test_cli_inspect_rejects_size_before_unbounded_read(tmp_path, monkeypatch):
    target = tmp_path / "invalid.pa9"
    target.write_bytes(b"x" * 345)
    def unbounded_read(*_args, **_kwargs):
        pytest.fail("PA9 inspect must not issue an unbounded read_bytes")
    monkeypatch.setattr(Path, "read_bytes", unbounded_read)
    with pytest.raises(ValueError):
        zatrade.cmd_inspect(SimpleNamespace(file=str(target), no_validate=False))


def _fifo_worker(path_text, scenario, output):
    path = Path(path_text)
    if scenario == "capture":
        os.mkfifo(path)
        operation = lambda: list(capture.iter_records(path))
    else:
        record = PA9(); record.species_internal = 25; record.update_checksum()
        path.write_bytes(record.to_bytes(encrypted=True))
        if scenario == "identity":
            real_lstat = Path.lstat
            swapped = False
            def swap_after_stat(self, *args, **kwargs):
                nonlocal swapped
                metadata = real_lstat(self, *args, **kwargs)
                if self == path and not swapped:
                    swapped = True
                    path.unlink(); os.mkfifo(path)
                return metadata
            Path.lstat = swap_after_stat
            operation = lambda: file_save.file_identity(path)
        else:
            real_identity = pa9_editor.file_identity
            swapped = False
            def swap_after_identity(value):
                nonlocal swapped
                identity = real_identity(value)
                if Path(value) == path and not swapped:
                    swapped = True
                    path.unlink(); os.mkfifo(path)
                return identity
            pa9_editor.file_identity = swap_after_identity
            operation = lambda: pa9_editor.load_pa9_document(path)
    output.send("armed")
    try:
        operation()
    except (OSError, ValueError, RuntimeError):
        output.send("rejected")
    else:
        output.send("accepted")
    finally:
        output.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO fault injection")
@pytest.mark.parametrize("scenario", ["capture", "identity", "editor-load"])
def test_file_reads_do_not_hang_on_special_files_or_path_swaps(tmp_path, scenario):
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    worker = ctx.Process(target=_fifo_worker, args=(str(tmp_path / "input.epa9"), scenario, child))
    worker.start(); child.close()
    try:
        assert parent.poll(10) and parent.recv() == "armed"
        worker.join(2)
        assert not worker.is_alive(), "blocked on FIFO instead of rejecting non-regular input"
        assert worker.exitcode == 0
        assert parent.poll(1) and parent.recv() == "rejected"
    finally:
        if worker.is_alive():
            worker.terminate(); worker.join(5)
        parent.close()


def test_delayed_start_callback_cannot_reroute_pending_trade_result(desktop, tmp_path):
    app, root, messages = desktop
    app.capture_output_var.set(str(tmp_path / "capture.jsonl"))
    app.link_code_var.set("12345678")
    _queue_uncertain_result(app, tmp_path)
    app._start_probe()  # a stale/reentrant callback must not overwrite active_operation
    assert app.active_operation == "trade"
    app._poll()
    assert messages.showwarning.call_count == 1
    assert not app.controller.has_pending_result


@pytest.mark.parametrize("closing", [False, True])
def test_analysis_admission_preserves_pending_worker_result(desktop, tmp_path, monkeypatch, closing):
    app, root, messages = desktop
    path = tmp_path / "capture.jsonl"; path.write_text('{"rec":"pkt"}\n')
    app.analyze_var.set(str(path))
    finished = Mock(); finished.is_alive.return_value = False
    app._offline_thread = finished
    app.close_when_idle = closing
    starts = []
    monkeypatch.setattr(threading.Thread, "start", lambda _self: starts.append(True))
    app._analyze_capture()
    assert not starts, "pending result was replaced by a new untracked worker"
    assert app._offline_thread is finished
    app._offline_thread = None
    app.close_when_idle = False


def test_controller_thread_start_failure_restores_idle(monkeypatch):
    controller = GuiController()
    def no_threads(_self):
        raise RuntimeError("synthetic thread creation failure")
    monkeypatch.setattr(threading.Thread, "start", no_threads)
    with pytest.raises(RuntimeError, match="synthetic"):
        controller._begin(lambda *_: pytest.fail("must not run"), "fault")
    assert controller.state is ControllerState.IDLE
    assert not controller.is_busy and not controller.has_pending_result
    assert controller.drain_events() == ()
    assert controller.wait(0)


def test_analysis_thread_start_failure_does_not_trap_gui_exit(desktop, tmp_path, monkeypatch):
    app, root, messages = desktop
    path = tmp_path / "capture.jsonl"; path.write_text('{"rec":"pkt"}\n')
    app.analyze_var.set(str(path))
    def no_threads(_self):
        raise RuntimeError("synthetic thread creation failure")
    monkeypatch.setattr(threading.Thread, "start", no_threads)
    app._analyze_capture()
    assert app._offline_thread is None and app._offline_cancel is None
    assert messages.showerror.called
    assert not app._pending_results()
    assert "disabled" not in app.analyze_button.state()



def test_exit_cancels_owned_callbacks_before_destroy(desktop, monkeypatch):
    app, root, _messages = desktop
    root.after(5000, lambda: None)
    pending_at_destroy=[]
    original=root.destroy
    def destroy():
        pending_at_destroy.extend(root.tk.call("after", "info"))
        original()
    monkeypatch.setattr(root,"destroy",destroy)
    app._request_close()
    assert pending_at_destroy==[], "Tk callbacks survive destruction and can fire into deleted widgets"


@pytest.mark.parametrize("limit", [-1, True, 1.5, "100"])
def test_regular_reader_rejects_invalid_limits_before_open(tmp_path, limit):
    with pytest.raises(ValueError, match="max_size"):
        file_save.read_regular_bytes(tmp_path / "absent", max_size=limit)


def test_regular_reader_bounds_the_read_even_if_file_grows(monkeypatch, tmp_path):
    from contextlib import contextmanager
    import io
    reads = []
    class GrowingInput(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)
    @contextmanager
    def opened(*_args, **_kwargs):
        with GrowingInput(b"x" * 1000) as stream:
            yield stream
    monkeypatch.setattr(file_save, "open_regular_binary", opened)
    with pytest.raises(ValueError, match="exceeds"):
        file_save.read_regular_bytes(tmp_path / "growth", max_size=32)
    assert reads == [33]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink swap and O_NOFOLLOW")
def test_regular_reader_rejects_symlink_raced_at_open(tmp_path, monkeypatch):
    target = tmp_path / "input.pa9"; target.write_bytes(b"original")
    other = tmp_path / "other.pa9"; other.write_bytes(b"not-for-this-read")
    real_open = file_save.os.open
    raced = []
    def open_after_swap(path, flags, *args, **kwargs):
        if Path(path) == target:
            raced.append(True)
            assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
            target.unlink(); target.symlink_to(other)
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(file_save.os, "open", open_after_swap)
    with pytest.raises(OSError):
        file_save.read_regular_bytes(target, max_size=100)
    assert raced == [True] and other.read_bytes() == b"not-for-this-read"


def test_regular_reader_closes_descriptor_on_midread_error(tmp_path):
    path = tmp_path / "file"; path.write_bytes(b"payload")
    fd = None
    with pytest.raises(RuntimeError, match="synthetic"):
        with file_save.open_regular_binary(path) as stream:
            fd = stream.fileno()
            raise RuntimeError("synthetic consumer failure")
    with pytest.raises(OSError):
        os.fstat(fd)


def test_json_nesting_limit_is_quote_aware_and_independent_of_recursion_limit():
    from zasim.json_limits import validate_json_depth
    import json
    validate_json_depth(b"[" * 64 + b"0" + b"]" * 64)
    with pytest.raises(ValueError, match="nesting"):
        validate_json_depth(b"[" * 65 + b"0" + b"]" * 65)
    validate_json_depth(json.dumps({"string": '[\\"' * 1000 + "}" * 1000}).encode())


@pytest.mark.parametrize("raw", [b"\xff", b"not json", b"[1,2]", b"x" * 65537])
def test_bad_preferences_preserved_and_startup_falls_back(tmp_path, monkeypatch, raw):
    target = tmp_path / "preferences.json"; target.write_bytes(raw)
    monkeypatch.setattr(i18n, "preference_path", lambda: target)
    monkeypatch.setattr(i18n, "detect_language", lambda: "ja")
    monkeypatch.delenv("ZA_TRADE_LANG", raising=False)
    assert i18n.preferred_language() == "ja"
    assert target.read_bytes() == raw



def test_readonly_diagnostic_thread_failure_restores_its_button(desktop, monkeypatch):
    app, root, _messages = desktop
    errors=[]
    root.report_callback_exception=lambda *args: errors.append(args)
    app._check_environment()
    def fail(_thread): raise RuntimeError("synthetic thread creation failure")
    monkeypatch.setattr(threading.Thread,"start",fail)
    app.environment_radio_button.invoke()
    assert errors==[], "thread start escaped into the Tk callback handler"
    assert "disabled" not in app.environment_radio_button.state()
    assert app._radio_diagnostic_thread is None



@pytest.mark.parametrize("ssid", [1, True, ["x"], {"x":1}])
def test_invalid_session_metadata_types_raise_validation_error(tmp_path, ssid):
    import json
    from zasim.capture import analyze_capture
    p=tmp_path/"input.jsonl"
    p.write_text(json.dumps({"rec":"meta","event":"session","ssid_hex":ssid})+"\n")
    with pytest.raises(ValueError): list(analyze_capture(p))

def test_invalid_session_metadata_cli_has_no_traceback(tmp_path):
    import json, subprocess, sys
    p=tmp_path/"input.jsonl"
    p.write_text(json.dumps({"rec":"meta","event":"session","ssid_hex":1})+"\n")
    r=subprocess.run([sys.executable,"-B","zatrade.py","analyze",str(p)],capture_output=True,text=True)
    assert r.returncode==2 and "Traceback" not in r.stderr
