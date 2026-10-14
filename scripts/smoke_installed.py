"""Exercise installed CLI and (optionally) mapped Tk entrypoints, without radio.

Run with the installed environment's python -I, outside the source tree.
All inputs are synthetic and all output goes to a temporary directory.
"""
from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--gui", action="store_true", help="requires a real desktop or owned Xvfb display")
    args = parser.parse_args(argv)
    version = metadata.version("za-ldn-trade")
    if version != args.expected_version:
        raise RuntimeError("installed package version mismatch")
    import zasim.gui as gui
    import zatrade
    from zasim.pa9 import PA9
    # A package imported from a source checkout cannot certify an installed wheel.
    prefix = Path(sys.prefix).resolve()
    for path in (Path(gui.__file__).resolve(), Path(zatrade.__file__).resolve()):
        if prefix not in path.parents:
            raise RuntimeError("smoke test imported the source tree instead of the installed package")
    checks = {"version": version, "installed_imports": True, "actual_radio_tested": False}
    with tempfile.TemporaryDirectory(prefix="za-installed-smoke-") as temporary:
        directory = Path(temporary)
        def cli(*arguments):
            result = subprocess.run([sys.executable, "-I", "-B", "-m", "zatrade", *arguments],
                                    cwd=directory, capture_output=True, text=True, timeout=20)
            if result.returncode:
                raise RuntimeError("installed CLI smoke failed: " + result.stderr[:2000])
            return result.stdout
        if cli("--version").strip() != "za-ldn-trade " + version:
            raise RuntimeError("installed CLI version mismatch")
        output = directory / "synthetic.pa9"
        cli("create-pa9", "--species", "152", "--language", "2", "--ot-name", "Test",
            "--tid", "1", "--sid", "2", "--output", str(output))
        mon = PA9(output.read_bytes())
        if mon.size != 344 or mon.stat_level != 5 or mon.stat_hp_max <= 0 or not mon.checksum_valid:
            raise RuntimeError("installed PA9 creation/calculation failed")
        json.loads(cli("inspect-pa9", str(output)))
        if "--output" not in cli("bss-helper", "build", "--help"):
            raise RuntimeError("installed optional-helper command missing")
        checks.update(cli_version=True, synthetic_PA9_creation_and_inspection=True, helper_command_present=True)
        if args.gui:
            original_init = gui._DesktopApp.__init__
            errors = []; callbacks = []; windows = []
            os.environ["XDG_CONFIG_HOME"] = str(directory / "preferences")
            def init(app, *positional, **kwargs):
                original_init(app, *positional, **kwargs)
                windows.append(app)
                def exercise():
                    try:
                        app.root.update_idletasks()
                        if not app.root.winfo_viewable():
                            raise RuntimeError("installed Tk window was not mapped")
                        if app.root.title() != "za-ldn-trade " + version:
                            raise RuntimeError("installed GUI product name mismatch")
                        if not app.change_language("ja") or not app.change_language("en"):
                            raise RuntimeError("installed language switch failed")
                        if app.root.title() != "za-ldn-trade " + version:
                            raise RuntimeError("language switch changed the GUI product name")
                        app.exit_button.invoke()
                        callbacks.extend(app.root.tk.call("after", "info"))
                    except BaseException as exc:
                        errors.append(exc)
                        try: app.root.destroy()
                        except Exception: pass
                app.root.after(200, exercise)
            gui._DesktopApp.__init__ = init
            try:
                # The registered entrypoint, not an unrelated source script.
                entry = next(e for e in metadata.entry_points(group="gui_scripts") if e.name == "za-ldn-trade-gui")
                result = entry.load()(backend="relay", language="en")
                if result not in (0, None) or len(windows) != 1 or callbacks or errors:
                    raise RuntimeError("installed GUI lifecycle failed") from (errors[0] if errors else None)
            finally:
                gui._DesktopApp.__init__ = original_init
            checks.update(installed_GUI_entrypoint=True, mapped_Tk=True, bilingual=True, product_name=True,
                          normal_exit=True, remaining_Tk_callbacks=0)
    print(json.dumps(checks, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
