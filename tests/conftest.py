"""Dispose cyclic Tk test objects on the thread that created the interpreter."""
import gc
import sys
import threading

import pytest


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_teardown(item, nextitem):
    # Wait for the test and ALL fixture finalizers (notably monkeypatch undo).
    # A destroyed window can still have unreachable Python Variable cycles;
    # leaving these for a later worker's automatic GC calls Tcl off-thread.
    # Do not destroy windows, remove traces, suppress warnings or change GC
    # settings here: product lifetime assertions must still run unmodified.
    try:
        yield
    finally:
        if "tkinter" in sys.modules:
            assert threading.current_thread() is threading.main_thread()
            gc.collect()
