"""Cross-caller and runtime-selection boundaries, no hardware/installed-env changes."""
import json
import os
from pathlib import Path
import subprocess
import threading
from unittest.mock import Mock

import pytest

from zasim import gui
from test_review_hardening import desktop, _party


def test_gui_inspect_reports_concurrent_file_change(desktop, tmp_path, monkeypatch):
    app,root,messages=desktop
    path=tmp_path/'input.epa9';path.write_bytes(_party(25))
    app.inspect_var.set(str(path))
    actual_open=os.open;reached=[]
    def swapped_open(target,flags,*args,**kwargs):
        if Path(target)==path and not reached:
            reached.append(True)
            replacement=tmp_path/'replacement';replacement.write_bytes(_party(133));replacement.replace(path)
        return actual_open(target,flags,*args,**kwargs)
    monkeypatch.setattr(os,'open',swapped_open)
    app._inspect_pa9()
    assert reached==[True] and messages.showerror.call_count==1
    assert 'changed' in messages.showerror.call_args.args[1]


def test_diagnostic_constructor_failure_restores_button(desktop,monkeypatch):
    app,root,messages=desktop
    callback_errors=[];root.report_callback_exception=lambda *args:callback_errors.append(args)
    app._check_environment()
    constructor=Mock(side_effect=RuntimeError('synthetic constructor failure'))
    monkeypatch.setattr(threading,'Thread',constructor)
    app.environment_radio_button.invoke()
    assert constructor.call_count==1 and not callback_errors
    assert 'disabled' not in app.environment_radio_button.state()
    assert app._radio_diagnostic_thread is None


@pytest.mark.skipif(os.name=='nt',reason='POSIX setup script')
@pytest.mark.parametrize('live',[False,True])
def test_setup_checks_existing_venv_runtime_before_pip(tmp_path, live):
    # Invoke the real shell launcher with synthetic interpreter adapters. It
    # must test the selected *existing venv*, not just the bootstrap Python.
    root=tmp_path/'application';scripts=root/'scripts';scripts.mkdir(parents=True)
    scripts.joinpath('setup_za.sh').write_bytes((Path(gui.__file__).parents[1]/'scripts/setup_za.sh').read_bytes())
    bootstrap=tmp_path/'bootstrap-python';bootstrap.write_text('#!/bin/sh\nexit 0\n');bootstrap.chmod(0o755)
    bindir=root/'.venv-za/bin';bindir.mkdir(parents=True)
    calls=tmp_path/'calls.jsonl'
    runtime=bindir/'python';runtime.write_text('#!/usr/bin/python3\nimport sys,json\nfrom pathlib import Path\nPath('+repr(str(calls))+').open("a").write(json.dumps(sys.argv[1:])+"\\n")\nif "-c" in sys.argv and "version_info" in sys.argv[-1]:\n print("existing environment is too old",file=sys.stderr);sys.exit(3)\n')
    runtime.chmod(0o755)
    proc=subprocess.run(['sh',str(scripts/'setup_za.sh')]+(['--live'] if live else []),env={**os.environ,'PYTHON':str(bootstrap)},text=True,capture_output=True,timeout=5)
    recorded=[json.loads(line) for line in calls.read_text().splitlines()]
    assert proc.returncode!=0, 'installer falsely succeeds with an unsupported existing venv'
    assert any('-c' in args and 'version_info' in args[-1] for args in recorded)
    assert not any('pip' in args for args in recorded), 'must reject before changing an incompatible environment'


def test_cancel_callbacks_handles_native_progressbar_timer(desktop):
    app,root,messages=desktop
    app.progress.start(10000)
    assert root.tk.call('after','info')
    app._cancel_callbacks()
    assert not root.tk.call('after','info')


def test_root_destroy_cancels_native_and_python_timers(desktop):
    app,root,messages=desktop
    errors=[];root.report_callback_exception=lambda *args:errors.append(args)
    root.after(60000,lambda:pytest.fail('cancelled timer ran'))
    app.progress.start(10000)
    root.destroy()
    assert errors==[] and not root.tk.call('after','info')


def test_failed_constructor_can_be_retried_once_without_phantom_worker(monkeypatch):
    from zasim.gui_controller import GuiController, ControllerState
    controller=GuiController()
    with monkeypatch.context() as patch:
        patch.setattr(threading,'Thread',Mock(side_effect=RuntimeError('constructor failed')))
        with pytest.raises(RuntimeError):controller._begin(lambda *_:None,'fault')
    done=threading.Event();seen=[]
    def work(job_id,cancel):
        seen.append(job_id)
        controller._finish(job_id,ControllerState.SUCCEEDED,'succeeded','done')
        done.set()
    job=controller._begin(work,'recovered')
    assert done.wait(5) and controller.wait(5)
    assert seen==[job]
    assert controller.drain_events()==(gui.GuiEvent(job,'succeeded','done'),)
    assert controller.acknowledge(job) and controller.state is ControllerState.IDLE
