"""Correct-outcome regressions, first executed against frozen 0.8.5."""
import json
import multiprocessing
import os
from pathlib import Path
import threading
from unittest.mock import Mock

import pytest

from zasim import capture
from zasim.gui_controller import ConfirmationPrompt, ControllerState, GuiController, GuiEvent
from test_review_hardening import desktop, _queue_uncertain_result


def _fifo_writer(path_text, output, scenario):
    path = Path(path_text)
    path.write_text('approved capture')
    identity = capture.capture_file_identity(path)
    stopped=[]
    if scenario=='writer':
        path.unlink(); os.mkfifo(path)
        def operation():
            with capture.CaptureWriter(path, overwrite=True, expected_identity=identity):
                pass
    else:
        from zasim.live import run_passive_probe
        class Transport:
            ssid=bytes(16)
            def start(self):
                path.unlink();os.mkfifo(path)
                return self
            def stop(self):
                stopped.append(True)
        operation=lambda:run_passive_probe(output=path,overwrite=True,
            expected_output_identity=identity,transport_factory=Transport,log=lambda _:None)
    output.send('armed')
    try:
        operation()
    except (OSError, ValueError):
        assert stopped==([] if scenario=='writer' else [True])
        output.send('rejected')
    else:
        output.send('accepted')
    finally:
        output.close()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX FIFO fault injection')
@pytest.mark.parametrize('scenario',['writer','probe'])
def test_capture_writer_rejects_substituted_fifo_without_blocking(tmp_path,scenario):
    ctx = multiprocessing.get_context('spawn')
    parent, child = ctx.Pipe(duplex=False)
    worker = ctx.Process(target=_fifo_writer, args=(str(tmp_path/'record.jsonl'),child,scenario))
    worker.start(); child.close()
    try:
        assert parent.poll(10) and parent.recv() == 'armed'
        worker.join(1)
        assert not worker.is_alive(), 'CaptureWriter blocked on a substituted FIFO'
        assert worker.exitcode == 0
        assert parent.poll(1) and parent.recv() == 'rejected'
    finally:
        if worker.is_alive():
            worker.terminate(); worker.join(5)
        parent.close()


@pytest.mark.parametrize('endpoint', [[':'], {':': 'bad'}])
def test_malformed_capture_endpoint_is_reported_per_packet(tmp_path, endpoint):
    path=tmp_path/'capture.jsonl'
    records=[{'rec':'meta','event':'session','ssid_hex':bytes(16).hex()},
             {'rec':'pkt','hex':'00','src':endpoint},
             {'rec':'pkt','hex':'00','src':'169.254.1.1:12345'}]
    path.write_text(''.join(json.dumps(r)+'\n' for r in records))
    result=list(capture.analyze_capture(path))
    assert len(result)==3 and all('error' in r for r in result[1:])


def test_transient_log_error_cannot_discard_terminal_result(desktop,tmp_path,monkeypatch):
    app, root, messages=desktop
    result=_queue_uncertain_result(app,tmp_path)
    app.controller._log(app.active_job_id,'ordinary log before terminal')
    real_insert=app.log_text.insert; reached=[]
    def fail_once(index, text):
        if text=='ordinary log before terminal\n' and not reached:
            reached.append(True)
            raise RuntimeError('synthetic log widget failure')
        return real_insert(index, text)
    monkeypatch.setattr(app.log_text,'insert',fail_once)
    app._poll(); app._poll()
    assert reached==[True]
    assert messages.showwarning.call_count==1, 'terminal event lost after log error'
    assert str(result.output_path) in messages.showwarning.call_args.args[1]
    assert not app.controller.has_pending_result


def test_poll_reschedules_even_if_error_logging_also_fails(desktop,tmp_path,monkeypatch):
    app,root,messages=desktop
    _queue_uncertain_result(app,tmp_path)
    monkeypatch.setattr(app,'_append_log',Mock(side_effect=RuntimeError('log unavailable')))
    messages.showwarning.side_effect=RuntimeError('dialog unavailable')
    original=root.after; scheduled=[]
    def schedule(*args):
        scheduled.append(args)
        return original(*args)
    monkeypatch.setattr(root,'after',schedule)
    app._poll()
    assert scheduled[-1]==(app.POLL_MS,app._poll)
    assert app.controller.has_pending_result and app._pending_terminal_event is not None


def test_offline_error_is_retained_until_dialog_is_shown(desktop,monkeypatch):
    app,root,messages=desktop
    app._offline_thread=Mock(is_alive=lambda:False)
    app._offline_events.put(('error',ValueError('synthetic malformed input')))
    messages.showerror.side_effect=[RuntimeError('transient native dialog failure'),None]
    app._poll()
    assert app._pending_results(), 'failed presentation silently discarded the analysis error'
    app._poll()
    assert messages.showerror.call_count==2 and not app._pending_results()


def test_invalid_offer_dialog_failure_still_rejects_confirmation(desktop):
    app,root,messages=desktop
    prompt=ConfirmationPrompt(0,b'bad-pa9')
    app.controller._pending_confirmation=prompt
    app.controller._state=ControllerState.AWAITING_CONFIRMATION
    messages.showerror.side_effect=RuntimeError('transient native dialog failure')
    try:
        app._handle_confirmation(GuiEvent(0,'confirmation',prompt))
    except RuntimeError:
        pass
    assert prompt._done.is_set(), 'worker left waiting after invalid PA9 display failed'
    assert prompt.wait(threading.Event()) is False


def test_controller_constructor_failure_restores_idle(monkeypatch):
    controller=GuiController()
    constructor=Mock(side_effect=RuntimeError('synthetic Thread constructor failure'))
    monkeypatch.setattr(threading,'Thread',constructor)
    with pytest.raises(RuntimeError,match='constructor'):
        controller._begin(lambda *_:None,'fault')
    assert constructor.call_count==1
    assert controller.state is ControllerState.IDLE and controller.wait(0)
    assert controller._cancel_event is None


def test_analysis_constructor_failure_restores_controls(desktop,tmp_path,monkeypatch):
    app,root,messages=desktop
    path=tmp_path/'capture.jsonl';path.write_text('{"rec":"pkt"}\n')
    app.analyze_var.set(str(path))
    constructor=Mock(side_effect=RuntimeError('synthetic Thread constructor failure'))
    monkeypatch.setattr(threading,'Thread',constructor)
    app._analyze_capture()
    assert constructor.call_count==1 and messages.showerror.called
    assert app._offline_thread is None and app._offline_cancel is None
    assert not app._pending_results() and 'disabled' not in app.analyze_button.state()


@pytest.mark.parametrize('endpoint', [False, True, 0, 12, 1.5, [], {}, [':'], {':': 0}])
def test_endpoint_input_type_is_validated(endpoint):
    with pytest.raises(ValueError, match='endpoint'):
        capture.endpoint_ip(endpoint)


@pytest.mark.parametrize('endpoint,expected', [
    (None,None), ('',None), ('169.254.1.2','169.254.1.2'),
    ('169.254.1.2:12345','169.254.1.2'),
])
def test_endpoint_valid_inputs_unchanged(endpoint,expected):
    assert capture.endpoint_ip(endpoint)==expected


@pytest.mark.skipif(os.name=='nt',reason='POSIX FIFO opened with an existing reader')
def test_capture_writer_checks_opened_type_before_truncation(tmp_path,monkeypatch):
    path=tmp_path/'capture.jsonl';os.mkfifo(path)
    reader=os.open(path,os.O_RDONLY|os.O_NONBLOCK)
    truncated=Mock();monkeypatch.setattr(os,'ftruncate',truncated)
    try:
        with pytest.raises(ValueError,match='regular file'):
            capture.CaptureWriter(path,overwrite=True,expected_identity=(1,2,3,4))
        assert not truncated.called
    finally:
        os.close(reader)


def test_capture_approved_overwrite_and_repeated_close(tmp_path):
    path=tmp_path/'capture.jsonl';path.write_text('approved old data')
    writer=capture.CaptureWriter(path,overwrite=True,expected_identity=capture.capture_file_identity(path))
    writer.packet('in',b'new',src='169.254.1.1:12345',dst='169.254.1.2:12345')
    writer.close();writer.close()
    records=list(capture.iter_records(path))
    assert len(records)==1 and records[0]['hex']==b'new'.hex()


@pytest.mark.parametrize('completed',[False,True])
def test_broken_log_widget_does_not_block_terminal_dialog(desktop,tmp_path,monkeypatch,completed):
    from dataclasses import replace
    app,root,messages=desktop
    original=_queue_uncertain_result(app,tmp_path)
    if completed:
        app.controller._events.clear()
        app.controller._finish(app.active_job_id,ControllerState.SUCCEEDED,'succeeded',replace(original,completed=True))
    monkeypatch.setattr(app,'_append_log',Mock(side_effect=RuntimeError('log widget unavailable')))
    app._poll()
    dialog=messages.showinfo if completed else messages.showwarning
    assert dialog.call_count==1 and str(original.output_path) in dialog.call_args.args[1]
    assert not app._pending_results()


def test_offline_result_render_is_retried_without_allowing_new_job(desktop,monkeypatch):
    app,root,messages=desktop
    app._offline_thread=Mock(is_alive=lambda:False)
    result=[{'record':123}];app._offline_events.put(('result',result))
    original=app._show_result;seen=[]
    def render(value):
        seen.append(value)
        if len(seen)==1:
            raise RuntimeError('synthetic result widget failure')
        return original(value)
    monkeypatch.setattr(app,'_show_result',render)
    app._poll()
    assert app._pending_results() and not app._can_start_operation()
    app._poll()
    assert seen==[result,result] and not app._pending_results()
    assert '123' in app.result_text.get('1.0','end')


def test_invalid_confirmation_batch_preserves_later_terminal_event(desktop,tmp_path):
    app,root,messages=desktop
    result=_queue_uncertain_result(app,tmp_path)
    # Simulate a callback failure before the terminal event in this same batch.
    app.controller._events.appendleft(GuiEvent(app.active_job_id,'confirmation',None))
    original=app._handle_confirmation;seen=[]
    def handle(event):
        seen.append(event)
        raise RuntimeError('synthetic confirmation rendering failure')
    app._handle_confirmation=handle
    app._poll();app._handle_confirmation=original;app._poll()
    assert len(seen)==1 and messages.showwarning.call_count==1
    assert str(result.output_path) in messages.showwarning.call_args.args[1]
    assert not app._pending_results()


def _lifetime_app(language):
    """No timer/trace pre-cleanup: observe the actual mapped app's exit."""
    from zasim.gui import _DesktopApp
    from test_gui import _create_tk_root
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
    root = _create_tk_root(tk)
    app = _DesktopApp(root, tk=tk, ttk=ttk, filedialog=filedialog,
        messagebox=messagebox, scrolledtext=scrolledtext, backend="relay",
        relay="127.0.0.1:24872", keys="unused", phyname="", ifname="ldnclient",
        nickname="Synthetic", language=language)
    root.update_idletasks()
    assert root.winfo_viewable()
    return root, app


@pytest.mark.parametrize("language", ["ja", "en"])
@pytest.mark.parametrize("exit_kind", ["normal", "direct"])
def test_mapped_app_reopen_releases_owned_traces_and_references(language, exit_kind):
    import gc
    import weakref
    for _ in range(5):
        root, app = _lifetime_app(language)
        assert sum(len(v.trace_info()) for v in app.editor_vars.values()) == 67
        if exit_kind == "normal":
            app.exit_button.invoke()
        else:
            root.destroy()
        assert not root.tk.call("after", "info")
        assert sum(len(v.trace_info()) for v in app.editor_vars.values()) == 0
        app_ref, root_ref = weakref.ref(app), weakref.ref(root)
        del app, root
        gc.collect()
        assert app_ref() is None and root_ref() is None


def test_trace_disposal_is_owned_idempotent_and_preserves_foreign_interpreter():
    import tkinter as tk
    from test_gui import _create_tk_root
    foreign = _create_tk_root(tk)
    foreign_var = tk.StringVar(master=foreign)
    foreign_calls = []
    foreign_trace = foreign_var.trace_add("write", lambda *_: foreign_calls.append("write"))
    timer = foreign.after(60000, lambda: None)
    try:
        root, app = _lifetime_app("en")
        variable = app.editor_vars["nickname"]
        other_trace = variable.trace_add("write", lambda *_: None)
        app._dispose_editor_traces()
        app._dispose_editor_traces()
        assert variable.trace_info() == [(("write",), other_trace)]
        app.exit_button.invoke()
        assert variable.trace_info() == [(("write",), other_trace)]
        assert timer in foreign.tk.call("after", "info")
        foreign_var.set("still alive")
        assert foreign_calls == ["write"] and foreign.winfo_exists()
        # These belong to the test, and are removed only after observation.
        variable.trace_remove("write", other_trace)
    finally:
        foreign_var.trace_remove("write", foreign_trace)
        foreign.after_cancel(timer)
        foreign.destroy()


@pytest.mark.parametrize("width", [128, 4096])
def test_log_slice_retains_order_drop_notice_and_terminal(desktop, tmp_path, monkeypatch, width):
    from zasim import gui
    from types import SimpleNamespace
    app, root, messages = desktop
    result = _queue_uncertain_result(app, tmp_path)
    # A stable clock proves the count/character bound independently of speed.
    monkeypatch.setattr(gui, "time", SimpleNamespace(monotonic=lambda: 0.0))
    for i in range(10000):
        app.controller._log(app.active_job_id, f"{i}:" + "x" * width)
    expected = "[gui] 8000 old log line(s) were discarded\n" + "".join(
        gui.display_safe(f"{i}:" + "x" * width, limit=4096) + "\n"
        for i in range(8000, 10000))
    inserted = []
    insert = app.log_text.insert
    def record(index, text):
        inserted.append(text)
        return insert(index, text)
    monkeypatch.setattr(app.log_text, "insert", record)
    app._poll()
    assert len(inserted) == 1
    assert 0 < len(inserted[0].splitlines()) <= app.LOG_SLICE_LINES
    assert app._pending_results() and not messages.showwarning.called
    assert not app.controller._logs  # one drained batch, not repeated accumulation
    for _ in range(2100):
        if not app._pending_results():
            break
        app._poll()
    assert not app._pending_results() and messages.showwarning.call_count == 1
    text = app.log_text.get("1.0", "end-1c")
    assert text.startswith(expected)
    assert str(result.output_path) in messages.showwarning.call_args.args[1]
    assert not app.controller.acknowledge(app.controller.job_id)
    assert app._log_lines <= app.MAX_LOG_LINES
    assert len(inserted) < 400  # batched Tcl calls, not 2000 per-message scrolls


def test_log_slice_time_budget_retains_unprocessed_lines(desktop, monkeypatch):
    from zasim import gui
    from types import SimpleNamespace
    app, root, messages = desktop
    clock = [0.0]
    monkeypatch.setattr(gui, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    original = gui.display_safe
    def costly(value, **kwargs):
        clock[0] += app.POLL_BUDGET_SECONDS
        return original(value, **kwargs)
    monkeypatch.setattr(gui, "display_safe", costly)
    app.controller._log(0, "first\nsecond\nthird")
    app._poll()
    assert app.log_text.get("1.0", "end-1c") == "first\n"
    assert list(app._pending_log_lines) == ["second", "third"]
    app._poll(); app._poll()
    assert app.log_text.get("1.0", "end-1c") == "first\nsecond\nthird\n"
    assert not app._pending_log_lines


def test_multiline_log_cap_escape_and_language_rebuild_keep_pending_lines(desktop):
    from zasim import gui
    app, root, messages = desktop
    source = [f"{i}:\ud800\u202e" + "x" * 4100 for i in range(5050)]
    app.controller._log(0, "\r\n".join(source) + "  \n")
    app._poll()
    assert app._pending_log_lines and app._log_lines <= app.LOG_SLICE_LINES
    # Rebuilding the widgets must not discard the in-flight slice/remainder.
    previous_widget = app.log_text
    language = "en" if app.language == "ja" else "ja"
    app.language_var.set(gui.LANGUAGE_NAMES[language])
    app._language_selected()
    assert app.language == language and app.log_text is not previous_widget
    for _ in range(5100):
        if not (app._pending_log_lines or app._pending_controller_events or app.controller._logs):
            break
        app._poll()
    expected = "".join(gui.display_safe(line, limit=4096) + "\n" for line in source[-5000:])
    assert app.log_text.get("1.0", "end-1c") == expected
    assert app._log_lines == 5000 and app.log_text.cget("state") == "disabled"


@pytest.mark.parametrize("failure_boundary", ["configure", "insert", "see"])
def test_batched_log_widget_errors_keep_terminal_retry_single_ack(desktop, tmp_path, monkeypatch, failure_boundary):
    app, root, messages = desktop
    result = _queue_uncertain_result(app, tmp_path)
    for _ in range(150):
        app.controller._log(app.active_job_id, "synthetic log")
    fault = Mock(side_effect=RuntimeError("synthetic widget failure"))
    monkeypatch.setattr(app.log_text, failure_boundary, fault)
    messages.showwarning.side_effect = [RuntimeError("synthetic dialog failure")] * 5 + [None]
    ack = Mock(wraps=app.controller.acknowledge)
    monkeypatch.setattr(app.controller, "acknowledge", ack)
    app._request_close()
    for _ in range(20):
        if ack.called:
            break
        app._poll()
    assert fault.called and messages.showwarning.call_count == 6
    assert len({call.args for call in messages.showwarning.call_args_list}) == 1
    assert str(result.output_path) in messages.showwarning.call_args.args[1]
    assert ack.call_count == 1 and not app._pending_results()
    assert not root.tk.call("after", "info")


def test_log_backlog_does_not_drain_again_or_overtake_confirmation(desktop, monkeypatch):
    from test_review_hardening import _party
    app, root, messages = desktop
    app.active_job_id = 0
    app.controller._state = ControllerState.AWAITING_CONFIRMATION
    prompt = ConfirmationPrompt(0, _party(25))
    app.controller._pending_confirmation = prompt
    for i in range(100):
        app.controller._log(0, str(i))
    app.controller._emit(0, "confirmation", prompt)
    app._poll()
    assert not prompt._done.is_set()
    for i in range(3000):
        app.controller._log(0, f"later:{i}")
    for _ in range(110):
        if prompt._done.is_set():
            break
        app._poll()
    assert prompt._done.is_set()
    assert "99\n" in app.log_text.get("1.0", "end-1c")
    assert len(app.controller._logs) == 2000 and app.controller._dropped_logs == 1000
    assert not app._pending_controller_events and not app._pending_log_lines


@pytest.mark.parametrize("language", ["ja", "en"])
@pytest.mark.parametrize("close_kind", ["cancel", "window_close", "accept_save", "root_destroy"])
def test_new_dialog_close_releases_its_trace_and_app_references(language, close_kind, tmp_path):
    import gc
    import weakref
    from tkinter import filedialog
    for cycle in range(3):
        root, app = _lifetime_app(language)
        app.notebook.select(app.editor_tab)
        app.editor_new_button.invoke()
        dialog = app.editor_new_dialog
        root.update_idletasks()
        assert dialog.window.winfo_viewable()
        assert len(dialog.vars["encounter"].trace_info()) == 1
        try:
            if close_kind == "accept_save":
                for name, value in {"ot_name": "Test", "tid": "1", "sid": "2", "language": "2"}.items():
                    dialog.vars[name].set(value)
                dialog.create_button.invoke()
                assert app.editor_document is not None and app.editor_document.dirty
                output = tmp_path / f"new-{language}-{cycle}.pa9"
                choose = filedialog.asksaveasfilename
                try:
                    filedialog.asksaveasfilename = lambda **_: str(output)
                    app.editor_save_as_button.invoke()
                finally:
                    filedialog.asksaveasfilename = choose
                assert output.read_bytes() == app.editor_document.record.data
                assert not app.editor_document.dirty
            elif close_kind == "cancel":
                dialog.cancel_button.invoke()
                assert app.editor_document is None
            elif close_kind == "window_close":
                dialog.window.tk.call(dialog.window.protocol("WM_DELETE_WINDOW"))
                assert app.editor_document is None
            else:
                root.destroy()
            assert not dialog.vars["encounter"].trace_info()
            if close_kind != "root_destroy":
                app.exit_button.invoke()
            assert not root.tk.call("after", "info")
            refs = [weakref.ref(obj) for obj in (dialog, app, root)]
        finally:
            try:
                root.destroy()
            except app.tk.TclError as exc:
                if "application has been destroyed" not in str(exc):
                    raise
        del dialog, app, root
        gc.collect()
        assert all(ref() is None for ref in refs)


def test_new_dialog_trace_disposal_preserves_foreign_trace_and_other_interpreter():
    from types import SimpleNamespace
    import tkinter as tk
    from tkinter import ttk
    from test_gui import _create_tk_root
    foreign = _create_tk_root(tk)
    timer = foreign.after(60000, lambda: None)
    root, app = _lifetime_app("en")
    app.editor_new_button.invoke()
    dialog = app.editor_new_dialog
    variable = dialog.vars["encounter"]
    calls = []
    other_trace = variable.trace_add("write", lambda *_: calls.append("write"))
    try:
        # Child Destroy events must not dispose the live dialog's mode callback.
        extra = ttk.Label(dialog.window)
        extra.destroy()
        variable.set("custom")
        assert len(variable.trace_info()) == 2
        assert len(dialog.widgets["species"]["values"]) == 490
        variable.set("starter")
        assert len(dialog.widgets["species"]["values"]) == 6
        # Rejected creation keeps the dialog and its trace usable.
        errors = []
        dialog.messagebox = SimpleNamespace(showerror=lambda *a, **kw: errors.append(a))
        dialog.create_button.invoke()
        assert errors and dialog.window.winfo_exists()
        assert app.editor_document is None and len(variable.trace_info()) == 2
        dialog.cancel_button.invoke()
        assert variable.trace_info() == [(('write',), other_trace)]
        dialog._destroyed(SimpleNamespace(widget=dialog.window))  # idempotent
        variable.set("after-close")
        assert len(calls) == 3 and variable.trace_info() == [(('write',), other_trace)]
        app.exit_button.invoke()
        assert timer in foreign.tk.call("after", "info") and foreign.winfo_exists()
    finally:
        # Only harness-owned state is cleaned here, after the assertions.
        variable.trace_remove("write", other_trace)
        try:
            root.destroy()
        except tk.TclError as exc:
            if "application has been destroyed" not in str(exc):
                raise
        foreign.after_cancel(timer)
        foreign.destroy()
