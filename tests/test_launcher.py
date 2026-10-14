from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

import pytest


POWERSHELL = shutil.which("pwsh") or shutil.which("powershell.exe")


@pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None,
    reason="Windows PowerShell launcher test requires Windows",
)
def test_start_za_relay_wsl_preflight_with_mocked_wsl() -> None:
    test_script = Path(__file__).with_name("test_start_za_relay_wsl.ps1")
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "mock tests passed" in result.stdout
