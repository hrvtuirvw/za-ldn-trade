"""Regression for GUI-test garbage reaching a later non-GUI worker."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


def test_tk_fixture_cycles_are_collected_after_finalizers_on_owner_thread(tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"mapped Tk display required: {exc}")
    root.destroy()
    probe = textwrap.dedent('''\
        import gc
        import threading
        import tkinter as tk
        import pytest

        OWNER = threading.get_ident()
        FINALIZED = []
        ROOTS = []
        # Deterministic scheduling control in this isolated test process only.
        gc.disable()

        class Witness(tk.StringVar):
            def __del__(self):
                FINALIZED.append(threading.get_ident())
                # The negative control records the forbidden thread instead
                # of spending seconds in Tcl's cross-thread wait or crashing.
                if threading.get_ident() == OWNER:
                    super().__del__()

        @pytest.fixture
        def gui_objects():
            root = tk.Tk()
            ROOTS.append(root)  # keep interpreter destruction on owner thread
            variable = Witness(master=root)
            variable.cycle = variable
            held = [variable]
            del variable
            yield held
            root.destroy()
            held.clear()  # cycle becomes garbage only in fixture finalization

        def test_01_gui_fixture(gui_objects):
            assert gui_objects and not FINALIZED

        def test_02_later_worker():
            worker = threading.Thread(target=gc.collect)
            worker.start()
            worker.join(2)
            assert not worker.is_alive()
            assert FINALIZED == [OWNER], "Tk garbage reached the later worker"
    ''')
    outcomes = []
    for with_cleanup in (False, True):
        case = tmp_path / ("owner-cleanup" if with_cleanup else "negative-control")
        case.mkdir()
        (case / "test_probe.py").write_text(probe, encoding="utf-8")
        if with_cleanup:
            (case / "conftest.py").write_bytes(Path(__file__).with_name("conftest.py").read_bytes())
        env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTEST_ADDOPTS="")
        result = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "--confcutdir=" + str(case), str(case / "test_probe.py")],
            cwd=case, env=env, capture_output=True, text=True, timeout=30,
        )
        outcomes.append(result)
    assert outcomes[0].returncode == 1, outcomes[0].stdout + outcomes[0].stderr
    assert "Tk garbage reached the later worker" in outcomes[0].stdout
    assert outcomes[1].returncode == 0, outcomes[1].stdout + outcomes[1].stderr
    assert "2 passed" in outcomes[1].stdout
